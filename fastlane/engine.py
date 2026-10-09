"""Fast-lane engine: news event -> shortlist -> Jev decision (book prefetched in parallel) -> paper fill -> marks.

Paper only. Nothing here can place a real order: there is no order endpoint anywhere in this module.
"""
import asyncio
import os
import time

import httpx

from fastlane.books import fetch_book, simulate_fill
from fastlane.decision import build_request, decide
from fastlane.feeds import FeedHub
from fastlane.jev_client import JevClient
from fastlane.kalshi import KalshiClient
from fastlane.kalshi_tape import KalshiTape
from fastlane.ledger import Ledger
from fastlane.universe import Universe

MARK_HORIZONS_S = [5, 30, 60, 300, 900, 3600]
PREFETCH_BOOKS = 8          # fetch every candidate book while Jev is thinking (requests are cheap, waits are not)
KEEPWARM_EVERY_S = 3        # upstreams drop idle connections after ~5 s; a cold call costs ~400 ms more
UNIVERSE_REFRESH_S = 15 * 60
MAX_NEWS_AGE_S = 600        # news first seen >10 min after publication is logged, never traded
PRICED_IN_MOVE = 0.03       # skip if the market already moved >=3c our way between publication and decision


def freshness_block(ev: dict, mid_at_published: float | None, mid_now: float | None, side: str) -> str | None:
    """'stale_news' | 'priced_in' | None. Only trade news that is new to the market, not just new to us.

    Synthetic events are never blocked.
    """
    if ev.get("synthetic"):
        return None
    pub = ev.get("published_ts")
    if pub and ev["seen_ts"] - pub > MAX_NEWS_AGE_S:
        return "stale_news"
    if mid_at_published is not None and mid_now is not None:
        moved_our_way = (mid_now - mid_at_published) if side == "yes" else (mid_at_published - mid_now)
        if moved_our_way >= PRICED_IN_MOVE:
            return "priced_in"
    return None


def _fmt_ms(x):
    return f"{x:6.0f}ms" if x is not None else "     - "


class Engine:
    def __init__(self, workers: int = 8, verbose: bool = True):
        self.bankroll = float(os.environ.get("PAPER_BANKROLL_USD", 10000))
        self.max_trade = self.bankroll * float(os.environ.get("PAPER_MAX_TRADE_PCT", 0.02))
        self.halt_loss = self.bankroll * float(os.environ.get("PAPER_DAILY_LOSS_HALT_PCT", 0.05))
        self.workers = workers
        self.verbose = verbose
        self.queue: asyncio.Queue = asyncio.Queue()
        self.feed_stats: dict = {}
        self.jev = JevClient()
        self.http = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(5.0, connect=3.0),
                                      limits=httpx.Limits(max_keepalive_connections=20))
        self.universe = Universe()
        self.ledger = Ledger()
        self.hub = FeedHub(self.queue, self.feed_stats)
        self.kalshi_ids: set[str] = set()
        self.by_id: dict[str, dict] = {}
        self.kalshi = KalshiClient()
        self.tape_enabled = self.kalshi.configured
        self.tape = KalshiTape(self.ledger, universe_ids=lambda: self.kalshi_ids,
                               market_info=self.by_id.get, on_move=self._on_move,
                               sign_headers=self.kalshi.sign_headers if self.tape_enabled else None)
        self.processed = 0
        self.trades = 0
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()

    # ---------- lifecycle ----------
    async def start(self, feeds: bool = True):
        t0 = time.perf_counter()
        async with httpx.AsyncClient(http2=True, timeout=30) as c:
            src = await self.universe.load(c)
        print(f"universe: {len(self.universe.markets):,} markets from {src} in {time.perf_counter() - t0:.1f}s")
        has_id, has_key = bool(os.environ.get("KALSHI_API_KEY_ID", "").strip()), \
            bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip())
        if has_id != has_key:
            print("warning: KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must both be set; Kalshi tape disabled")
        elif not self.tape_enabled:
            print("warning: no Kalshi API key, live tape disabled (no move detector, no price-at-publish, "
                  "no priced-in guard). Public Kalshi market data still works.")
        self._index_kalshi()
        if self.tape_enabled:
            self._tasks.append(asyncio.create_task(self.tape.run()))
        await self._keepwarm_once()
        self._tasks += [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        self._tasks.append(asyncio.create_task(self._keepwarm_loop()))
        self._tasks.append(asyncio.create_task(self._refresh_universe_loop()))
        if feeds:
            feed_tasks = self.hub.tasks()
            self._tasks += feed_tasks
            print(f"feeds: {len(feed_tasks)} pollers started (first poll of each = backlog, not traded)")

    async def stop(self):
        pending = self._tasks + list(self._bg)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.hub.aclose()
        await self.jev.aclose()
        await self.http.aclose()

    async def _keepwarm_once(self):
        t0 = time.perf_counter()
        await asyncio.gather(
            self.jev.keepalive(),
            self.http.get("https://api.elections.kalshi.com/trade-api/v2/exchange/status"),
            self.http.get("https://clob.polymarket.com/time"),
            return_exceptions=True)
        return (time.perf_counter() - t0) * 1000

    async def _keepwarm_loop(self):
        while True:
            await asyncio.sleep(KEEPWARM_EVERY_S)
            await self._keepwarm_once()

    async def _refresh_universe_loop(self):
        while True:
            await asyncio.sleep(UNIVERSE_REFRESH_S)
            try:
                async with httpx.AsyncClient(http2=True, timeout=30) as c:
                    await self.universe.load(c, force=True)
                self._index_kalshi()
            except Exception as exc:
                print(f"universe refresh failed: {exc}")

    def _index_kalshi(self):
        self.kalshi_ids = {m["id"] for m in self.universe.markets if m["venue"] == "kalshi"}
        self.by_id.clear()
        self.by_id.update((m["id"], m) for m in self.universe.markets)
        if not self.tape_enabled:
            return  # a stale snapshot must not feed the priced-in guard
        # Seed the tape with the universe snapshot so quiet markets still have a "price at publish time".
        for m in self.universe.markets:
            if m["venue"] == "kalshi" and m.get("yes_ask") and not self.tape.hist.get(m["id"]):
                self.tape.hist[m["id"]].append((self.universe.snapshot_ts, m["yes_bid"], m["yes_ask"]))

    def _on_move(self, ticker: str, info: dict, before: tuple, now: tuple):
        """A liquid Kalshi market just repriced: treat it as breaking news for *other* markets."""
        b_mid, n_mid = (before[1] + before[2]) / 2, (now[1] + now[2]) / 2
        headline = (f'Prediction market "{info["question"]}" just repriced from {b_mid * 100:.0f}% to '
                    f'{n_mid * 100:.0f}% within {now[0] - before[0]:.0f} seconds')
        ev = {"id": f"move-{ticker}-{int(now[0])}", "source": "kalshi_move", "headline": headline,
              "summary": "", "url": "", "published_ts": now[0], "seen_ts": time.time(),
              "exclude_event": ticker.rsplit("-", 1)[0]}
        self.queue.put_nowait(ev)

    def _tape_mid(self, ticker: str, ts: float | None):
        q = self.tape.quote_at(ticker, ts) if ts and self.tape_enabled else None
        return round((q[1] + q[2]) / 2, 4) if q else None

    async def _worker(self):
        while True:
            ev = await self.queue.get()
            try:
                await self.handle(ev)
            except Exception as exc:
                print(f"handle error on {ev.get('headline', '')[:60]}: {exc!r}")
            finally:
                self.queue.task_done()

    # ---------- the hot path ----------
    async def handle(self, ev: dict) -> dict:
        self.ledger.event(ev)
        t0 = time.perf_counter()
        cands = self.universe.shortlist(f"{ev['headline']} {ev.get('summary', '')}")
        if ev.get("exclude_event"):  # a market move should not "predict" its own strike ladder
            cands = [m for m in cands if not m["id"].startswith(ev["exclude_event"])]
        shortlist_ms = (time.perf_counter() - t0) * 1000
        rec = {"n_candidates": len(cands), "shortlist_ms": shortlist_ms}

        if not cands:
            rec.update(action="PASS", reason="no_candidates", total_ms=(time.perf_counter() - t0) * 1000)
            return self._finish(ev, rec)

        prefetch = {m["id"]: self._spawn(fetch_book(self.http, m)) for m in cands[:PREFETCH_BOOKS]}
        for m in cands:  # live Kalshi quotes beat the up-to-15-minute-old cache when ranking room to profit
            q = self.tape.quote(m["id"]) if m["venue"] == "kalshi" and self.tape_enabled else None
            if q:
                m["yes_bid"], m["yes_ask"] = q[1], q[2]
        state, questions, keyed = build_request(ev, cands)
        t1 = time.perf_counter()
        try:
            res = await self.jev.decide(state, questions)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            res = {"error": "network", "detail": repr(exc)}
        rec["jev_ms"] = (time.perf_counter() - t1) * 1000

        if "error" in res:
            for t in prefetch.values():
                t.cancel()
            err = res["error"]
            if err == "network":
                reason = "jev_error"
            elif isinstance(err, (str, int)) and not isinstance(err, bool):
                reason = f"jev_error_{err}"
            else:
                reason = "jev_error_api"
            rec.update(action="PASS", reason=reason, total_ms=(time.perf_counter() - t0) * 1000)
            return self._finish(ev, rec)

        answers = res.get("answers", {})
        d = decide(answers, keyed)
        action, reason = d["action"], d["reason"]
        chosen = keyed.get(d.get("key"))
        rec.update(action=action, reason=reason, answers=answers, jev_cost=(res.get("usage") or {}).get("cost"),
                   market_conf=d.get("strength"), p_up=d.get("p_yes_side"), p_down=d.get("p_no_side"),
                   materiality=d.get("p_decisive"))

        book = None
        if chosen:
            rec.update(venue=chosen["venue"], market_id=chosen["id"], market_question=chosen["question"])
            t2 = time.perf_counter()
            task = prefetch.pop(chosen["id"], None) or self._spawn(fetch_book(self.http, chosen))
            try:
                book = await task
            except Exception:
                book = None
            rec["book_ms"] = (time.perf_counter() - t2) * 1000  # ~0 when the prefetch already landed
            if book:
                rec["mid_at_decision"] = book.mid()
            if chosen["venue"] == "kalshi":
                rec["mid_at_published"] = self._tape_mid(chosen["id"], ev.get("published_ts"))
                rec["mid_at_seen"] = self._tape_mid(chosen["id"], ev["seen_ts"])
                if self.tape_enabled:
                    self.tape.track(chosen["id"], since_ts=(ev.get("published_ts") or ev["seen_ts"]) - 120)
        for t in prefetch.values():
            t.cancel()

        if action != "PASS" and not chosen:  # a buy that cannot happen must not read as a buy
            action, rec["action"], rec["reason"] = "PASS", "PASS", "unknown_market"
        elif action != "PASS" and not book:
            action, rec["action"], rec["reason"] = "PASS", "PASS", "no_book"

        fill = None
        if action != "PASS" and book:
            side = "yes" if action == "BUY_YES" else "no"
            blocked = freshness_block(ev, rec.get("mid_at_published"), book.mid(), side) or self._risk_block(chosen["id"], ev.get("synthetic", False))
            if blocked:
                rec["reason"] = blocked
            else:
                fill = simulate_fill(book, side, self.max_trade)
                if fill:
                    self.ledger.trade(event_id=ev["id"], opened_ts=time.time(), venue=chosen["venue"],
                                      market_id=chosen["id"], market_question=chosen["question"], side=side,
                                      contracts=fill["contracts"], avg_price=fill["avg_price"], cost=fill["cost"],
                                      fee=fill["fee"], best_ask=fill["best_ask"],
                                      synthetic=int(ev.get("synthetic", False)))
                    self.trades += 1
                else:
                    rec["reason"] = "no_fill_within_limit"

        rec["total_ms"] = (time.perf_counter() - t0) * 1000
        out = self._finish(ev, rec, fill)
        if chosen and book:
            self.ledger.mark(ev["id"], 0, book.best("yes"), book.bid("yes"), book.mid())
            self._spawn(self._marks(ev["id"], chosen))
        return out

    def _spawn(self, coro) -> asyncio.Task:
        """Run a background coroutine, tracked so stop() can cancel it."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _risk_block(self, market_id: str, synthetic: bool) -> str | None:
        if synthetic:
            return None
        if market_id in self.ledger.traded_markets():
            return "already_in_market"
        if self.ledger.spent_today() + self.max_trade > self.bankroll:
            return "bankroll_exhausted"
        if self._today_pnl() < -self.halt_loss:
            return "daily_loss_halt"
        return None

    def _today_pnl(self) -> float:
        rows = self.ledger.db.execute("""
            SELECT t.side, t.contracts, t.cost, t.fee,
                   (SELECT yes_bid FROM marks m WHERE m.event_id = t.event_id ORDER BY horizon_s DESC LIMIT 1),
                   (SELECT yes_ask FROM marks m WHERE m.event_id = t.event_id ORDER BY horizon_s DESC LIMIT 1)
            FROM trades t WHERE t.synthetic = 0 AND t.opened_ts > ?""", (time.time() - 86400,)).fetchall()
        pnl = 0.0
        for side, n, cost, fee, yb, ya in rows:
            exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
            if exit_px is not None:
                pnl += n * exit_px - cost - fee
        return pnl

    async def _marks(self, event_id: str, market: dict):
        start = time.time()
        for h in MARK_HORIZONS_S:
            await asyncio.sleep(max(0.0, start + h - time.time()))
            try:
                b = await fetch_book(self.http, market)
                self.ledger.mark(event_id, h, b.best("yes"), b.bid("yes"), b.mid())
            except Exception:
                pass

    def _finish(self, ev: dict, rec: dict, fill: dict | None = None) -> dict:
        self.ledger.decision(ev["id"], **rec)
        self.processed += 1
        if self.verbose:
            lag = ev["seen_ts"] - ev["published_ts"] if ev.get("published_ts") else None
            lag_s = f"{lag:7.0f}s" if lag is not None else "      ?"
            tag = "SYN " if ev.get("synthetic") else ""
            line = (f"{tag}[{ev['source'][:13]:13}] src-lag {lag_s} | match {rec['shortlist_ms']:5.1f}ms "
                    f"jev {_fmt_ms(rec.get('jev_ms'))} book-wait {_fmt_ms(rec.get('book_ms'))} "
                    f"total {_fmt_ms(rec.get('total_ms'))} | {rec['action']:7} {rec['reason']:20} | {ev['headline'][:70]}")
            print(line)
            if rec.get("market_question"):
                print(f"{'':16}-> {rec.get('venue')}: {rec['market_question'][:90]}  "
                      f"(yes-side {rec.get('p_up') or 0:.2f}, no-side {rec.get('p_down') or 0:.2f}, "
                      f"decisive {rec.get('materiality') or 0:.2f})")
            if fill:
                print(f"{'':16}** PAPER FILL {rec['action']} {fill['contracts']} @ {fill['avg_price']} "
                      f"cost ${fill['cost']} fee ${fill['fee']}")
        return rec

    def status(self) -> str:
        bad = [f"{n}({s['errors']})" for n, s in self.feed_stats.items() if s["errors"] and not s["polls"]]
        polls = sum(s["polls"] for s in self.feed_stats.values())
        new = sum(s["new"] for s in self.feed_stats.values())
        if self.tape_enabled:
            tape = f"tape {'up' if self.tape.connected else 'DOWN'} {self.tape.msgs:,} quotes, {self.tape.moves} moves"
        else:
            tape = "tape off (no Kalshi key)"
        return (f"[status] polls {polls} | new items {new} | decided {self.processed} | paper trades {self.trades} "
                f"| queue {self.queue.qsize()} | {tape}" + (f" | dead feeds: {', '.join(bad)}" if bad else ""))
