"""Resting paper orders (ENTRY_STYLE=post): a bid placed inside the spread that fills only if the market comes to us.

Paper only. Nothing here talks to an exchange except GET order-book snapshots (books.fetch_book); the module has no way
to place a real order, and the engine's real-money path (Engine._real_order) is never called from here: `on_fill` only
lets the engine schedule price marks.

Fill model (books.post_fill): conservative, we are last in the queue at our price. A fill happens only when the best ask
of our side in a fresh book snapshot is at or below our limit; the quantity is the displayed size at or below the limit,
and the price is our limit, never better. A Kalshi ticker tick can wake a snapshot early but never fills by itself (the
displayed size is unknown from a tick).

Statuses: working -> filled | partial_expired | post_expired | withdrawn.
"""
import asyncio
import dataclasses
import json
import os
import time

from fastlane import books, errors
from fastlane.books import Book, kalshi_taker_fee, post_fill, post_limit, post_size
from fastlane.ledger import mark_key

POST_MAX_WAIT_S = 300      # env POST_MAX_WAIT_S: a resting order older than this expires
POST_POLL_S = 2.0          # env POST_POLL_S (min 1.0): book snapshot cadence per working order
POST_MAX_WORKING = 20      # env POST_MAX_WORKING: refuse new orders beyond this (reason post_queue_full)
STATUSES = ("working", "filled", "partial_expired", "post_expired", "withdrawn")
EPS = 1e-9


def order_settings(env=None) -> tuple[float, float, int]:
    """(max_wait_s, poll_s, max_working) from POST_MAX_WAIT_S / POST_POLL_S / POST_MAX_WORKING."""
    e = env if env is not None else os.environ

    def num(name: str, default: float) -> float:
        raw = (e.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return (max(num("POST_MAX_WAIT_S", POST_MAX_WAIT_S), 1.0), max(num("POST_POLL_S", POST_POLL_S), 1.0),
            max(int(num("POST_MAX_WORKING", POST_MAX_WORKING)), 0))


class PaperOrders:
    def __init__(self, ledger, http, fetch_book=books.fetch_book, on_fill=None, now=time.time, watch: set | None = None,
                 min_entry: float = books.MIN_ENTRY_PRICE):
        self.ledger = ledger
        self.http = http
        self.fetch_book = fetch_book
        self.on_fill = on_fill            # callable(order_row, trade_id, market): the engine schedules marks
        self.now = now
        self.watch: set[str] = watch if watch is not None else set()   # Kalshi tickers with a working order (tape hook)
        self.min_entry = min_entry
        self.max_wait_s, self.poll_s, self.max_working = order_settings()
        self._live: dict[int, dict] = {}       # working order id -> {"market_id", "side", "limit_price", "venue"}
        self._markets: dict[int, dict] = {}    # working order id -> universe market dict (needed to fetch its book)
        self._pending: set[int] = set()        # ids with a tick-triggered snapshot already scheduled
        self._tasks: set[asyncio.Task] = set()
        # order id -> {ask price: displayed quantity already credited at that price}. A resting order we see again in the
        # next snapshot is the SAME liquidity (our phantom bid never removes it), so only an increase is credited.
        self._seen: dict[int, dict[float, float]] = {}

    # ---------- placing ----------
    def full(self) -> bool:
        return len(self._live) >= self.max_working

    def place(self, *, book: str, ev: dict, market: dict, side: str, bk: Book, size_usd: float,
              strength, decisive, synthetic: bool) -> dict | None:
        """Write a working order at the post limit. None (and no row) when there is no limit or no whole contract to
        buy; the caller then reports no_room / longshot / no_exit_liquidity from the cost filter as it does for a take."""
        limit = post_limit(bk, side, self.min_entry)
        if limit is None:
            return None
        n = post_size(market["venue"], limit, size_usd)
        if n <= 0:
            return None
        t = self.now()
        row = {"book": book, "event_id": ev["id"], "venue": market["venue"], "market_id": market["id"],
               "market_question": market.get("question"), "side": side, "style": "post", "limit_price": limit,
               "take_price": bk.best(side), "requested": n, "filled": 0.0, "status": "working",
               "created_ts": t, "expires_ts": t + self.max_wait_s, "updated_ts": t,
               "signal_strength": strength, "signal_decisive": decisive, "synthetic": int(bool(synthetic))}
        row["id"] = self.ledger.order_place(**row)
        self._live[row["id"]] = {"market_id": market["id"], "side": side, "limit_price": limit, "venue": market["venue"]}
        self._markets[row["id"]] = market
        if market["venue"] == "kalshi":
            self.watch.add(market["id"])
        return row

    def working(self) -> list[dict]:
        return self.ledger.orders_working()

    # ---------- tape hook ----------
    def on_tick(self, ticker: str, ts: float, yes_bid: float, yes_ask: float) -> None:
        """Kalshi tape hook. For each working order on `ticker`: the ask of our side from the tick (yes: yes_ask;
        no: 1 - yes_bid). If it is at or below the limit, schedule an immediate book snapshot for that order instead of
        waiting for the next poll. The tick alone never fills (size unknown)."""
        for oid, o in list(self._live.items()):
            if o["market_id"] != ticker or oid in self._pending:
                continue
            if o["side"] == "yes":
                ask = yes_ask if yes_ask and yes_ask > 0 else None
            else:
                ask = round(1 - yes_bid, 4) if yes_bid and yes_bid > 0 else None
            if ask is None or ask > o["limit_price"] + EPS:
                continue
            try:
                task = asyncio.get_running_loop().create_task(self._check(oid))
            except RuntimeError:
                continue   # no loop (sync caller): the next poll takes the snapshot
            self._pending.add(oid)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ---------- polling ----------
    async def run(self) -> None:
        """Loop every POST_POLL_S: one book fetch per market with working orders, shared across its orders; apply the fill
        model, expire past expires_ts. Errors on one market never stop the loop."""
        try:
            while True:
                await asyncio.sleep(self.poll_s)
                await self.poll_once()
        finally:
            for t in list(self._tasks):
                t.cancel()

    async def poll_once(self) -> None:
        by_market: dict[str, list[int]] = {}
        for oid, o in list(self._live.items()):
            by_market.setdefault(o["market_id"], []).append(oid)

        async def snap(ids: list[int]):
            t_fetch = self.now()          # the snapshot is as old as the moment we asked for it
            try:
                market = self._markets.get(ids[0])
                bk = await self.fetch_book(self.http, market) if market is not None else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.capture(exc, "orders.poll")
                bk = None
            return ids, bk, t_fetch

        # concurrent fetches: one slow book must not delay the others (or age their snapshots)
        for ids, bk, t_fetch in await asyncio.gather(*(snap(ids) for ids in by_market.values())):
            for oid in ids:
                try:
                    self._expire_if_due(oid, self.now())
                    if bk is not None:
                        self._apply(oid, bk, t_fetch)
                    self._expire_if_due(oid, self.now())
                except Exception as exc:
                    errors.capture(exc, "orders.apply")

    async def _check(self, order_id: int) -> None:
        """One immediate snapshot for one order (woken by a tick)."""
        try:
            o = self._live.get(order_id)
            market = self._markets.get(order_id)
            if o is None or market is None:
                return
            t_fetch = self.now()
            bk = await self.fetch_book(self.http, market)
            self._expire_if_due(order_id, self.now())
            self._apply(order_id, bk, t_fetch)
            self._expire_if_due(order_id, self.now())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.capture(exc, "orders.check")
        finally:
            self._pending.discard(order_id)

    # ---------- fills and expiry ----------
    def _apply(self, order_id: int, bk: Book, ts: float) -> None:
        row = self.ledger.order_get(order_id)
        if not row or row["status"] != "working" or ts >= row["expires_ts"]:
            return                      # a snapshot taken at or after expiry never fills
        seen = self._seen.setdefault(row["id"], {})
        side_asks = bk.yes_asks if row["side"] == "yes" else bk.no_asks
        fresh = [(p, max(0.0, q - seen.get(p, 0.0))) for p, q in side_asks]       # only the increase over what we credited
        adj = dataclasses.replace(bk, **({"yes_asks": fresh} if row["side"] == "yes" else {"no_asks": fresh}))
        fill = post_fill(row["side"], row["limit_price"], row["requested"] - row["filled"], adj, row["venue"])
        if fill:
            left = fill["contracts"]
            for p, q in fresh:
                if p <= row["limit_price"] + EPS and left > 0:
                    take = min(left, q)
                    seen[p] = seen.get(p, 0.0) + take
                    left -= take
            self._record_fill(row, fill, bk, ts)

    def _record_fill(self, row: dict, fill: dict, bk: Book, ts: float) -> None:
        n_new, price = fill["contracts"], fill["price"]
        total = round(row["filled"] + n_new, 4)
        cost = round(total * price, 2)
        # Kalshi rounds the fee per fill: sum the fills, never recompute on the aggregate
        fee = round((row["fee"] or 0.0) + kalshi_taker_fee(n_new, price), 2) if row["venue"] == "kalshi" else 0.0
        asks = bk.yes_asks if row["side"] == "yes" else bk.no_asks
        evidence = json.dumps({"asks": [[p, q] for p, q in asks if p <= price + EPS][:5]})
        self.ledger.fill_add(order_id=row["id"], ts=ts, contracts=n_new, price=price, evidence=evidence,
                             ask_seen=fill["ask_seen"], qty_seen=fill["qty_seen"])
        done = total >= row["requested"] - EPS
        upd = {"filled": total, "avg_price": price, "cost": cost, "fee": fee, "updated_ts": ts}
        if done:
            upd.update(status="filled", closed_ts=ts)
        trade_id = row["trade_id"]
        first = trade_id is None
        if first:
            trade_id = self.ledger.trade(
                event_id=row["event_id"], opened_ts=ts, venue=row["venue"], market_id=row["market_id"],
                market_question=row["market_question"], side=row["side"], contracts=total, avg_price=price, cost=cost,
                fee=fee, best_ask=row["take_price"], synthetic=0, shadow=int(row["book"] == "shadow"),
                signal_strength=row["signal_strength"], signal_decisive=row["signal_decisive"], book=row["book"],
                entry_style="post", order_id=row["id"], limit_price=row["limit_price"])
            upd["trade_id"] = trade_id
            key = mark_key(row["event_id"], row["book"])
            if not self.ledger.db.execute("SELECT 1 FROM marks WHERE event_id = ? AND horizon_s = 0", (key,)).fetchone():
                self.ledger.mark(key, 0, bk.best("yes"), bk.bid("yes"), bk.mid())   # the live book already has one
        else:
            self.ledger.trade_update(trade_id, contracts=total, avg_price=price, cost=cost, fee=fee)
        self.ledger.order_update(row["id"], **upd)
        if done:
            self._forget(row["id"])
        if first and self.on_fill:
            try:
                self.on_fill({**row, **upd}, trade_id, self._markets.get(row["id"]) or
                             {"venue": row["venue"], "id": row["market_id"], "question": row["market_question"]})
            except Exception as exc:   # the engine's mark scheduling must not undo a recorded fill
                errors.capture(exc, "orders.on_fill")

    def _expire_if_due(self, order_id: int, ts: float) -> None:
        row = self.ledger.order_get(order_id)
        if not row or row["status"] != "working" or ts < row["expires_ts"]:
            return
        status = "partial_expired" if row["filled"] > 0 else "post_expired"
        self.ledger.order_update(order_id, status=status, closed_ts=ts, updated_ts=ts)
        self._forget(order_id)

    def _forget(self, order_id: int) -> None:
        o = self._live.pop(order_id, None)
        self._markets.pop(order_id, None)
        self._seen.pop(order_id, None)
        if o and not any(x["market_id"] == o["market_id"] for x in self._live.values()):
            self.watch.discard(o["market_id"])

    def withdraw_all(self, reason: str) -> int:
        """Engine stop (or restart cleanup): every working order becomes withdrawn. Returns how many."""
        t = self.now()
        rows = self.ledger.orders_working()
        for row in rows:
            self.ledger.order_update(row["id"], status="withdrawn", note=reason, closed_ts=t, updated_ts=t)
            self._forget(row["id"])
        self._live.clear()
        self._markets.clear()
        self._seen.clear()
        self.watch.clear()
        return len(rows)
