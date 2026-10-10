"""Fast-lane engine: news event -> shortlist -> Jev decision (book prefetched in parallel) -> paper fill -> marks.

Paper by default. The only path to a real order is `self.live` (fastlane/live.py), and it stays closed unless the
user enabled real trading in .env AND flipped the dashboard switch for this engine session.
Shadow mode is a runtime switch read from the ledger (`Engine.shadow_enabled`, cached 2 s) and never touches the
real or the live path. The starter book (env STARTER_*) is a third paper book with the same isolation.
Every paper book enters through one method (`Engine._execute`): guards first, then either a take fill (cross the spread
now) or a resting paper order (the orders module). Only a LIVE-book take fill can reach `_real_order`.
"""
import asyncio
import os
import signal
import sqlite3
import time

import httpx

from fastlane import backup, errors, live, releases
from fastlane.bluesky import BlueskyFeed
from fastlane.books import COST_BLOCK_REASONS, TICK, Book, cost_block, fetch_book, post_limit, simulate_fill
from fastlane.config import KALSHI_PROD_BASE, kalshi_api_url, kalshi_base_url
from fastlane.decision import build_request, cost_settings, decide, entry_styles, shadow_settings, starter_settings
from fastlane.feeds import FeedHub
from fastlane.jev_client import JevClient
from fastlane.kalshi import KalshiClient
from fastlane.kalshi_tape import KalshiTape, move_deny_re
from fastlane.ledger import BOOK_SQL, Ledger, mark_key, shadow_enabled_from
from fastlane.live import LiveTrader
from fastlane.orders import PaperOrders
from fastlane.ratelimit import JevBudget, env_float
from fastlane.universe import Universe, is_sports_market
from fastlane.x_feed import XFeed, days_label

MARK_HORIZONS_S = [5, 30, 60, 300, 900, 3600]
PREFETCH_BOOKS = 8          # fetch every candidate book while Jev is thinking (requests are cheap, waits are not)
KEEPWARM_EVERY_S = 3        # upstreams drop idle connections after ~5 s; a cold call costs ~400 ms more
UNIVERSE_REFRESH_S = 15 * 60
MAX_NEWS_AGE_S = 600        # news first seen >10 min after publication is logged, never traded
PRICED_IN_MOVE = 0.03       # skip if the market already moved >=3c our way between publication and decision
HEARTBEAT_EVERY_S = 15      # engine_state.json: the dashboard only offers the real-money switch to a live engine
BALANCE_EVERY_S = 300


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


def _quotes_cell(rec: dict) -> str:
    w = rec.get("quote_wait_ms")
    return f"{w:.0f}ms/{rec.get('n_live_quotes', 0)}" if w is not None else "-"


def install_stop_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> list[int]:
    """SIGTERM and SIGINT set `stop` so run.py can leave its loop and run Engine.stop() (which clears the heartbeat).
    The first signal also restores the default handlers, so a second Ctrl-C (or SIGTERM) during a slow shutdown forces
    exit. Falls back to signal.signal plus call_soon_threadsafe where the loop cannot add signal handlers. Returns the
    signals. (A signal that arrives while Engine.start() runs is honoured once start returns.)"""
    sigs = (signal.SIGTERM, signal.SIGINT)
    use_loop = {}

    def restore():
        for sig in sigs:
            if use_loop.get(sig):
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass
            else:
                signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT else signal.SIG_DFL)

    def first():
        restore()
        stop.set()

    installed = []
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, first)
            use_loop[sig] = True
        except (NotImplementedError, RuntimeError):
            use_loop[sig] = False
            signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(first))
        installed.append(int(sig))
    return installed


class PrefetchPool:
    """The prefetched book tasks that the shadow and starter passes share (read only: nobody pops a task another pass may
    still need). Each pass calls acquire() when it is handed the pool and release() when it finishes; the LAST release
    cancels whatever is still in flight."""

    def __init__(self, tasks: dict[str, asyncio.Task]):
        self.tasks = tasks
        self.holders = 0

    def get(self, market_id: str) -> asyncio.Task | None:
        return self.tasks.get(market_id)

    def acquire(self) -> None:
        self.holders += 1

    def release(self) -> None:
        self.holders = max(0, self.holders - 1)
        if self.holders == 0:
            self.cancel_all()

    def cancel_all(self) -> None:
        for t in self.tasks.values():
            t.cancel()


class Engine:
    def __init__(self, workers: int = 8, verbose: bool = True, ledger=None, jev=None):
        self.bankroll = float(os.environ.get("PAPER_BANKROLL_USD", 10000))
        self.max_trade = self.bankroll * float(os.environ.get("PAPER_MAX_TRADE_PCT", 0.02))
        self.halt_loss = self.bankroll * float(os.environ.get("PAPER_DAILY_LOSS_HALT_PCT", 0.05))
        _, self.shadow_signal, self.shadow_decisive = shadow_settings()
        self._shadow_on = shadow_settings()[0]      # env fallback until the first ledger read
        self._shadow_checked_ts = 0.0
        self.shadow_check_s = 2.0                   # re-read the ledger setting at most this often
        self.max_spread, self.cost_to_room_max, self.min_entry = cost_settings()
        self.workers = workers
        self.verbose = verbose
        self.queue: asyncio.Queue = asyncio.Queue()
        self.feed_stats: dict = {}
        self.jev = jev if jev is not None else JevClient()
        self.http = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(5.0, connect=3.0),
                                      limits=httpx.Limits(max_keepalive_connections=20))
        self.universe = Universe()
        self.ledger = ledger if ledger is not None else Ledger()
        self.hub = FeedHub(self.queue, self.feed_stats)
        self.xfeed = XFeed(self.queue, self.feed_stats, self.ledger)       # off without XAI_API_KEY
        self.bsky = BlueskyFeed(self.queue, self.feed_stats, self.ledger)  # keyless; BSKY_ENABLED=false turns it off
        self.kalshi_ids: set[str] = set()
        self.by_id: dict[str, dict] = {}
        self.kalshi = KalshiClient()
        self.budget = JevBudget(self.ledger)
        self.live = LiveTrader(self.ledger, self.kalshi, self.http)
        self.live_orders = 0
        self.tape_enabled = self.kalshi.configured
        self.deny_re = move_deny_re()      # count and price ladders: no move event, and not a candidate of one
        self.tape = KalshiTape(self.ledger, universe_ids=lambda: self.kalshi_ids,
                               market_info=self.by_id.get, on_move=self._on_move,
                               sign_headers=self.kalshi.sign_headers if self.tape_enabled else None,
                               deny_re=self.deny_re,
                               on_tick=lambda ticker, ts, bid, ask: self.orders.on_tick(ticker, ts, bid, ask))
        self.styles = entry_styles()                # {"live", "shadow", "starter"} -> "take" | "post"
        self.quote_wait_s = max(env_float("LIVE_QUOTE_WAIT_MS", 150), 0.0) / 1000
        self.starter_on, self.starter_signal, self.starter_decisive, self.starter_size = starter_settings()
        # Late-bound fetch so a patched/replaced module-level fetch_book is honoured by the resting-order and release paths.
        self.orders = PaperOrders(self.ledger, self.http, fetch_book=lambda c, m: fetch_book(c, m),
                                  on_fill=self._on_paper_fill, watch=self.tape.watch, min_entry=self.min_entry)
        self.releases = releases.ReleaseScheduler(self.ledger, self.http, self.trade_release,
                                                  lambda c, m: fetch_book(c, m), cfg=releases.settings(),
                                                  quote_wait_s=self.quote_wait_s, verbose=verbose)
        self.processed = 0
        self.trades = 0
        self.shadow_trades = 0
        self.starter_trades = 0
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self._shadow_tasks: set[asyncio.Task] = set()
        self._starter_tasks: set[asyncio.Task] = set()
        self._armed_style_noted = False

    @property
    def shadow_enabled(self) -> bool:
        """Runtime shadow switch: ledger `settings.shadow_enabled` wins, else env SHADOW_ENABLED. Cached 2 s.

        Read only on the shadow branch, after the real decision, fill, marks and decision row are recorded, so the
        real path never waits on it. One primary-key lookup on the engine's own connection."""
        now = time.monotonic()
        if now - self._shadow_checked_ts >= self.shadow_check_s:
            try:
                self._shadow_on = shadow_enabled_from(self.ledger.db)[0]
            except sqlite3.OperationalError:
                pass   # transient read error: keep the previous cached value, retry in 2 s
            self._shadow_checked_ts = now
        return self._shadow_on

    # ---------- lifecycle ----------
    async def start(self, feeds: bool = True):
        t0 = time.perf_counter()
        base = kalshi_base_url()   # raises on a bad KALSHI_BASE_URL before anything is signed or sent
        if base != KALSHI_PROD_BASE:
            print(f"warning: Kalshi calls go to {base}, not production")
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
        self.live.start()   # new session, mode reset to paper: real trading never survives a restart
        if self.live.enabled:
            print("real trading: ENABLED in .env, but this session starts on PAPER. Flip the dashboard switch to go "
                  "real." if self.kalshi.configured else
                  "warning: LIVE_TRADING_ENABLED=1 but no Kalshi API key; real trading is unavailable")
        if self.tape_enabled:
            self._tasks.append(asyncio.create_task(self.tape.run()))
        await self._keepwarm_once()
        self._tasks += [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        self._tasks.append(asyncio.create_task(self._keepwarm_loop()))
        self._tasks.append(asyncio.create_task(self._refresh_universe_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._backup_loop()))
        stale = self.orders.withdraw_all("restart")   # a resting paper order never survives a restart
        if stale and self.verbose:
            print(f"paper orders: {stale} working order(s) from the previous session withdrawn")
        self._spawn(self.orders.run())
        rs = self.releases.cfg
        if rs.enabled:
            if not rs.bls_key:
                print("releases: BLS_API_KEY not set, CPI and jobs releases are disabled (FOMC releases still run)")
            self._spawn(self.releases.run())
        else:
            print("releases off (RELEASES_ENABLED=false)")
        if feeds:
            feed_tasks = self.hub.tasks()
            self._tasks += feed_tasks
            print(f"feeds: {len(feed_tasks)} pollers started (first poll of each = backlog, not traded)")
            if self.xfeed.enabled:
                s = self.xfeed.settings
                self._tasks.append(asyncio.create_task(self.xfeed.run()))
                print(f"x feed: {len(s.handles)} handles every {s.poll_seconds:.0f}s, window {s.start:%H:%M}-{s.end:%H:%M} "
                      f"{s.tz.key} {'every day' if len(s.days) == 7 else 'on ' + days_label(s.days)}, budget ${s.budget_usd:.2f}/UTC day")
            else:
                self.ledger.feed_status_set("x", connected=False, info={"enabled": False})   # never stay stuck at enabled
                print("x feed off (no XAI_API_KEY)")
            if self.bsky.enabled:
                self._tasks.append(asyncio.create_task(self.bsky.run()))
                print(f"bluesky: {len(self.bsky.settings.handles)} handles over Jetstream (polling fallback)")
            else:
                print("bluesky off (BSKY_ENABLED=false)")

    async def stop(self):
        self.live.stop()
        pending = self._tasks + list(self._bg)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.orders.withdraw_all("engine_stop")
        # Each close is independent: one failing must not skip the others or the shutdown backup below.
        for name, closer in (("hub", self.hub.aclose), ("xfeed", self.xfeed.aclose), ("bsky", self.bsky.aclose),
                             ("jev", self.jev.aclose), ("http", self.http.aclose)):
            try:
                await closer()
            except Exception as exc:
                errors.capture(exc, f"engine.stop.{name}")
        try:
            if self._backs_up():
                await asyncio.to_thread(backup.backup, self.ledger.path)
        except Exception as exc:
            errors.capture(exc, "backup.shutdown")

    async def _keepwarm_once(self):
        t0 = time.perf_counter()
        await asyncio.gather(
            self.jev.keepalive(),
            self.http.get(kalshi_api_url() + "/exchange/status"),
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
                errors.capture(exc, "universe.refresh")

    async def _heartbeat_loop(self):
        last_balance = 0.0
        while True:
            try:
                if time.time() - last_balance > BALANCE_EVERY_S:
                    last_balance = time.time()
                    await self.live.refresh_balance()
                self.live.heartbeat()
            except Exception as exc:
                errors.capture(exc, "engine.heartbeat")
            await asyncio.sleep(HEARTBEAT_EVERY_S)

    def _backs_up(self) -> bool:
        """Only the real ledger is backed up (tests and benchmarks use temp ledgers)."""
        return getattr(self.ledger, "path", None) == backup.DB_PATH and backup.DB_PATH.exists()

    async def _backup_loop(self):
        every = env_float("BACKUP_EVERY_HOURS", 6) * 3600
        if every <= 0:
            return
        while True:
            await asyncio.sleep(every)
            try:
                if self._backs_up():
                    path = await asyncio.to_thread(backup.backup, self.ledger.path)
                    if self.verbose:
                        print(f"backup: {path}")
            except Exception as exc:
                errors.capture(exc, "backup.scheduled")

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
              "exclude_series": ticker.split("-", 1)[0]}
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
                errors.capture(exc, "engine.handle")
            finally:
                self.queue.task_done()

    # ---------- the hot path ----------
    async def _live_quotes(self, prefetch: dict[str, asyncio.Task], cands: list[dict]) -> tuple[dict[str, Book], float, int]:
        """Wait at most self.quote_wait_s for the prefetched books. Books that landed (and did not raise) are returned by
        market id and removed from `prefetch`; tasks still pending stay in `prefetch` untouched (not cancelled) for the
        chosen-book step and the shadow/starter passes, and so do tasks that raised (their error is re-raised to whoever
        awaits them, so a failing market costs one fetch, not two). A wait of 0 still collects tasks that already
        finished. Returns (books, waited_ms, n_books)."""
        pending = {t for t in prefetch.values() if not t.done()}
        waited = 0.0
        if pending:
            t0 = time.perf_counter()
            await asyncio.wait(pending, timeout=self.quote_wait_s)
            waited = (time.perf_counter() - t0) * 1000
        landed: dict[str, Book] = {}
        for mid, task in list(prefetch.items()):
            if not task.done():
                continue
            if task.cancelled() or task.exception() is not None:
                continue            # keep it: awaiting it later raises, and counts as the one fetch for that market
            landed[mid] = task.result()
            del prefetch[mid]
        return landed, waited, len(landed)

    async def handle(self, ev: dict) -> dict:
        self.ledger.event(ev)
        t0 = time.perf_counter()
        cands = self.universe.shortlist(f"{ev['headline']} {ev.get('summary', '')}")
        if ev.get("exclude_series"):  # a market move must not "predict" its own series (any event of the same ladder family)
            cands = [m for m in cands if not (m["venue"] == "kalshi" and m["id"].split("-", 1)[0] == ev["exclude_series"])]
        if ev.get("source") == "kalshi_move":   # a count or price ladder is never a candidate of a move event (news events keep them)
            cands = [m for m in cands if not (self.deny_re.search(m.get("question") or "") or self.deny_re.search(str(m.get("id") or "")))]
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
        capped = self.budget.block()
        if capped:  # JEV_MAX_CALLS_PER_HOUR / JEV_MAX_USD_PER_DAY: no call, no spend
            for t in prefetch.values():
                t.cancel()
            rec.update(action="PASS", reason=capped, total_ms=(time.perf_counter() - t0) * 1000)
            return self._finish(ev, rec)
        state, questions, keyed = build_request(ev, cands)
        t1 = time.perf_counter()
        try:
            res = await self.jev.decide(state, questions)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            res = {"error": "network", "detail": repr(exc)}
        rec["jev_ms"] = (time.perf_counter() - t1) * 1000
        self.budget.record((res.get("usage") or {}).get("cost") if isinstance(res, dict) else None)

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
        # Rank on LIVE books, not on the cache: the books were fetched while Jev thought, so they have usually landed.
        live_books, rec["quote_wait_ms"], rec["n_live_quotes"] = await self._live_quotes(prefetch, cands)
        quotes = {k: {"yes_ask": b.best("yes"), "yes_bid": b.bid("yes")} for k, m in keyed.items()
                  if (b := live_books.get(m["id"])) is not None}
        d = decide(answers, keyed, quotes=quotes)
        action, reason = d["action"], d["reason"]
        real_intent = d["action"]   # the real rule's own call, before any rewrite to PASS (no_book, unknown_market, ...)
        chosen = keyed.get(d.get("key"))
        rec.update(action=action, reason=reason, answers=answers, jev_cost=(res.get("usage") or {}).get("cost"),
                   market_conf=d.get("strength"), p_up=d.get("p_yes_side"), p_down=d.get("p_no_side"),
                   materiality=d.get("p_decisive"))

        book = None
        if chosen:
            rec.update(venue=chosen["venue"], market_id=chosen["id"], market_question=chosen["question"])
            t2 = time.perf_counter()
            book = live_books.get(chosen["id"])
            if book is None:
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

        if action != "PASS" and not chosen:  # a buy that cannot happen must not read as a buy
            action, rec["action"], rec["reason"] = "PASS", "PASS", "unknown_market"
        elif action != "PASS" and not book:
            action, rec["action"], rec["reason"] = "PASS", "PASS", "no_book"

        fill = None
        if action != "PASS" and book:
            side = "yes" if action == "BUY_YES" else "no"
            blocked, res_ = await self._execute(
                "live", ev, chosen, side, book, self.max_trade, strength=d.get("strength"), decisive=d.get("p_decisive"),
                mid_at_published=rec.get("mid_at_published"), style=self.styles["live"])
            if blocked:
                rec["reason"] = blocked
                if blocked in COST_BLOCK_REASONS:
                    action = rec["action"] = "PASS"   # market-property filters: no buy intent recorded
            elif res_["style"] == "post":
                rec["reason"] = "post_working"        # a resting paper order, not a fill: the trade appears when it fills
            else:
                fill = res_
                rec["total_ms"] = (time.perf_counter() - t0) * 1000   # headline to paper fill, before the Kalshi round trip
                await self._real_order(ev, chosen, side, fill)

        rec.setdefault("total_ms", (time.perf_counter() - t0) * 1000)
        out = self._finish(ev, rec, fill)
        if chosen and book:
            self.ledger.mark(ev["id"], 0, book.best("yes"), book.bid("yes"), book.mid())
            self._spawn(self._marks(ev["id"], chosen))
        # Prefetch tasks were started before the Jev call and have usually landed by the time Jev answers. The shadow and
        # starter passes start strictly after the real decision row, fill and marks are recorded, and anything that may
        # await a network book runs as a tracked background task, so a slow fetch can never hold a worker. They share the
        # still-pending tasks through a pool; the last pass to finish cancels what is left.
        pool = PrefetchPool(prefetch)
        for name, setup in (("shadow", self._shadow), ("starter", self._starter)):
            try:
                setup(ev, rec, real_intent, answers, keyed, quotes, live_books, chosen, book, pool)
            except Exception as exc:  # a side-book bug must never look like a real-path failure
                print(f"{name} setup error (real decision already recorded) on {ev.get('headline', '')[:60]}: {exc!r}")
                errors.capture(exc, f"engine.{name}_setup")
        if pool.holders == 0:
            pool.cancel_all()
        return out

    async def drain_shadow(self):
        """Wait for in-flight shadow passes (tests and orderly shutdown helpers)."""
        while self._shadow_tasks:
            await asyncio.gather(*list(self._shadow_tasks), return_exceptions=True)

    async def drain_starter(self):
        """Wait for in-flight starter passes."""
        while self._starter_tasks:
            await asyncio.gather(*list(self._starter_tasks), return_exceptions=True)

    def _shadow(self, ev: dict, rec: dict, real_intent: str, answers: dict, keyed: dict, quotes: dict, live_books: dict,
                chosen: dict | None, book, pool: PrefetchPool) -> bool:
        """Record what the looser shadow rule would have done, in the shadow book. Never touches the real book.

        Runs on the same Jev answers (no extra call). Only when the real rule had no buy intent: if the real decide()
        returned a BUY (even one later rewritten to PASS: no_book, unknown_market, too_expensive) or the recorded action
        is not PASS, shadow records real_signalled and never trades. Paper only.

        This part is synchronous. Returns True when the rest was handed to a background task (which then holds the
        prefetch pool and releases it).
        """
        if not self.shadow_enabled or ev.get("synthetic"):
            return False
        if real_intent != "PASS" or rec["action"] != "PASS":
            rec.update(shadow_action=None, shadow_reason="real_signalled")
            self.ledger.shadow_decision(ev["id"], None, "real_signalled")
            return False
        pool.acquire()
        task = self._spawn(self._shadow_run(ev, rec, answers, keyed, chosen, book, pool, quotes, live_books))
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)
        return True

    async def _shadow_run(self, ev: dict, rec: dict, answers: dict, keyed: dict, chosen: dict | None, book,
                          pool: PrefetchPool, quotes: dict | None = None, live_books: dict | None = None):
        try:
            await self._shadow_decide(ev, rec, answers, keyed, chosen, book, pool, quotes, live_books)
        except Exception as exc:
            print(f"shadow error (real decision already recorded) on {ev.get('headline', '')[:60]}: {exc!r}")
            errors.capture(exc, "engine.shadow")
        finally:
            pool.release()

    async def _shadow_decide(self, ev: dict, rec: dict, answers: dict, keyed: dict, chosen: dict | None, book,
                             pool: PrefetchPool, quotes: dict | None = None, live_books: dict | None = None):
        def record(sa, sr, market_id):
            rec.update(shadow_action=sa, shadow_reason=sr, shadow_market_id=market_id)
            self.ledger.shadow_decision(ev["id"], sa, sr, market_id)

        await self._side_book("shadow", ev, rec, answers, keyed, quotes or {}, live_books or {}, chosen, book, pool,
                              self.shadow_signal, self.shadow_decisive, self.max_trade, record)

    def _starter(self, ev: dict, rec: dict, real_intent: str, answers: dict, keyed: dict, quotes: dict, live_books: dict,
                 chosen: dict | None, book, pool: PrefetchPool) -> bool:
        """The starter book: a small fixed-size paper book on a stricter-than-shadow rule (env STARTER_*). Same shape as
        the shadow pass: only when the real rule had no buy intent, never on synthetic events, never a real order."""
        if not self.starter_on or ev.get("synthetic"):
            return False
        if real_intent != "PASS" or rec["action"] != "PASS":
            rec.update(starter_action=None, starter_reason="real_signalled")
            self.ledger.starter_decision(ev["id"], None, "real_signalled")
            return False
        pool.acquire()
        task = self._spawn(self._starter_run(ev, rec, answers, keyed, chosen, book, pool, quotes, live_books))
        self._starter_tasks.add(task)
        task.add_done_callback(self._starter_tasks.discard)
        return True

    async def _starter_run(self, ev: dict, rec: dict, answers: dict, keyed: dict, chosen: dict | None, book,
                           pool: PrefetchPool, quotes: dict, live_books: dict):
        try:
            await self._starter_decide(ev, rec, answers, keyed, chosen, book, pool, quotes, live_books)
        except Exception as exc:
            print(f"starter error (real decision already recorded) on {ev.get('headline', '')[:60]}: {exc!r}")
            errors.capture(exc, "engine.starter")
        finally:
            pool.release()

    async def _starter_decide(self, ev: dict, rec: dict, answers: dict, keyed: dict, chosen: dict | None, book,
                              pool: PrefetchPool, quotes: dict, live_books: dict):
        def record(sa, sr, market_id):
            rec.update(starter_action=sa, starter_reason=sr, starter_market_id=market_id)
            self.ledger.starter_decision(ev["id"], sa, sr, market_id)

        await self._side_book("starter", ev, rec, answers, keyed, quotes, live_books, chosen, book, pool,
                              self.starter_signal, self.starter_decisive, self.starter_size, record)

    async def _side_book(self, name: str, ev: dict, rec: dict, answers: dict, keyed: dict, quotes: dict, live_books: dict,
                         chosen: dict | None, book, pool: PrefetchPool, sig: float, dec: float, size_usd: float, record):
        """One side-book decision (shadow or starter): the real scoring and room ranking with its own thresholds on the
        same live quotes, then the shared guards and entry in `_execute`. No bankroll or daily-halt check: these books
        have no bankroll."""
        ds = decide(answers, keyed, quotes=quotes, signal_threshold=sig, decisive_min=dec)
        sa, sr = ds["action"], ds["reason"]
        s_chosen = keyed.get(ds.get("key"))
        s_id = s_chosen["id"] if s_chosen else None
        if sa == "PASS":
            return record("PASS", sr, s_id)
        if not s_chosen:
            return record("PASS", "unknown_market", s_id)
        s_book = book if (chosen and book and s_id == chosen["id"]) else live_books.get(s_id)
        if s_book is None:
            try:
                task = pool.get(s_id) or self._spawn(fetch_book(self.http, s_chosen))
                s_book = await task
            except Exception:
                s_book = None
            if s_book is None:
                return record("PASS", "no_book", s_id)
        side = "yes" if sa == "BUY_YES" else "no"
        same = bool(chosen and s_id == chosen["id"])
        if same:
            mid_pub = rec.get("mid_at_published")
        else:
            mid_pub = self._tape_mid(s_id, ev.get("published_ts")) if s_chosen["venue"] == "kalshi" else None
        blocked, res = await self._execute(name, ev, s_chosen, side, s_book, size_usd, strength=ds.get("strength"),
                                           decisive=ds.get("p_decisive"), mid_at_published=mid_pub,
                                           style=self.styles[name])
        if blocked:
            return record("PASS" if blocked in COST_BLOCK_REASONS else sa, blocked, s_id)
        tail = ("" if same else "  (differs from the real decision's market)")
        if res["style"] == "post":
            record(sa, "post_working", s_id)
            if self.verbose:
                print(f"{'':16}** {name.upper()} ORDER RESTING {sa} {res['requested']:g} @ <= {res['limit_price']} "
                      f"(take would be {res['take_price']}) signal {ds['strength']:.2f}\n"
                      f"{'':16}   {name} market -> {s_chosen['venue']}: {s_chosen['question'][:90]}" + tail)
            return
        record(sa, sr, s_id)
        if self.verbose:
            what = "looser rule, not a real paper trade" if name == "shadow" else "starter book, not a real paper trade"
            print(f"{'':16}** {name.upper()} FILL {sa} {res['contracts']} @ {res['avg_price']} cost ${res['cost']} "
                  f"fee ${res['fee']} signal {ds['strength']:.2f} ({what})\n"
                  f"{'':16}   {name} market -> {s_chosen['venue']}: {s_chosen['question'][:90]}" + tail)

    # ---------- one entry path for every book ----------
    def _book_risk_block(self, book_name: str, market: dict, synthetic: bool) -> str | None:
        if book_name == "live":
            return self._risk_block(market["id"], synthetic)
        if book_name == "starter" and (is_sports_market(market) or market.get("category") == "Sports"):
            return "sports_market"
        if market["id"] in self.ledger.traded_markets(book_name):   # held, or a resting order on it
            return "already_in_market"
        return None

    def _effective_style(self, book_name: str, market: dict, synthetic: bool, style: str) -> str:
        """Synthetic events always take (a resting order in a one-minute test run would never fill). With real money armed
        the LIVE book takes on Kalshi whatever the env says, so a resting paper order can never diverge from the
        immediate-or-cancel order that would be sent."""
        if synthetic:
            return "take"
        if book_name == "live" and market["venue"] == "kalshi" and self.live.is_live():
            if style == "post" and not self._armed_style_noted:
                self._armed_style_noted = True
                print("real money armed: LIVE book uses take on Kalshi (mirrors the IOC order)")
            return "take"
        return style

    async def _execute(self, book_name: str, ev: dict, market: dict, side: str, bk: Book, size_usd: float, *,
                       strength: float | None, decisive: float | None, mid_at_published: float | None,
                       style: str, source_tag: str | None = None) -> tuple[str | None, dict | None]:
        """Guards in this order: freshness_block, cost_block, position/risk block for `book_name`, then either a take fill
        (simulate_fill + ledger.trade + marks) or a resting paper order (self.orders.place). Returns
        (blocked_reason, result); the result carries style "take" (a fill: contracts, avg_price, cost, fee, limit) or
        "post" (the order row). Never sends a real order: the caller decides that, only for the LIVE book on a take fill.
        There is no await between the guards and the ledger write, so concurrent events cannot double-enter a market."""
        synthetic = bool(ev.get("synthetic"))
        blocked = (freshness_block(ev, mid_at_published, bk.mid(), side)
                   or cost_block(bk, side, self.max_spread, self.cost_to_room_max, self.min_entry)
                   or self._book_risk_block(book_name, market, synthetic))
        if blocked:
            return blocked, None
        style = self._effective_style(book_name, market, synthetic, style)
        if style == "post":
            if self.orders.full():
                return "post_queue_full", None
            if post_limit(bk, side, self.min_entry) is None:
                bid = bk.bid(side)
                return ("longshot" if bid is not None and bid + TICK < self.min_entry else "priced_in"), None
            order = self.orders.place(book=book_name, ev=ev, market=market, side=side, bk=bk, size_usd=size_usd,
                                      strength=strength, decisive=decisive, synthetic=synthetic)
            if order is None:
                return "no_fill_within_limit", None
            return None, order
        fill = simulate_fill(bk, side, size_usd)
        if not fill:
            return "no_fill_within_limit", None
        self.ledger.trade(event_id=ev["id"], opened_ts=time.time(), venue=market["venue"], market_id=market["id"],
                          market_question=market["question"], side=side, contracts=fill["contracts"],
                          avg_price=fill["avg_price"], cost=fill["cost"], fee=fill["fee"], best_ask=fill["best_ask"],
                          synthetic=int(synthetic), shadow=int(book_name == "shadow"), book=book_name,
                          entry_style="take", limit_price=fill["limit"], signal_strength=strength, signal_decisive=decisive)
        if book_name == "live":
            self.trades += 1
        else:
            if book_name == "shadow":
                self.shadow_trades += 1
            else:
                self.starter_trades += 1
            key = mark_key(ev["id"], book_name)   # the LIVE book's marks are written by handle() for every decision
            self.ledger.mark(key, 0, bk.best("yes"), bk.bid("yes"), bk.mid())
            self._spawn(self._marks(key, market))
            if market["venue"] == "kalshi" and self.tape_enabled:
                self.tape.track(market["id"], since_ts=(ev.get("published_ts") or ev["seen_ts"]) - 120)
        return None, {**fill, "style": "take"}

    def _on_paper_fill(self, order: dict, trade_id: int, market: dict) -> None:
        """A resting paper order just got its first fill (the trade row exists and so does the horizon-0 mark). Paper only:
        schedule the price marks and count it. This never reaches the real-money path."""
        book = order["book"]
        if book == "live":
            self.trades += 1       # the LIVE decision already scheduled its marks
        else:
            if book == "shadow":
                self.shadow_trades += 1
            else:
                self.starter_trades += 1
            self._spawn(self._marks(mark_key(order["event_id"], book), market))
        if market.get("venue") == "kalshi" and self.tape_enabled:
            self.tape.track(order["market_id"], since_ts=order["created_ts"] - 120)
        if self.verbose:
            print(f"{'':16}** POST FILL [{book}] {order['side'].upper()} {order['filled']:g} @ {order['limit_price']} "
                  f"(take was {order['take_price']}): {(order.get('market_question') or '')[:80]}")

    # ---------- scheduled data releases ----------
    async def trade_release(self, ev: dict, market: dict, side: str, bk: Book, value, n_candidates: int = 1,
                            style: str | None = None) -> dict:
        """A number just came out and `market` settles on it: enter in the LIVE paper book (no Jev). Same sizing, same
        guards, same entry style and the same route to a real order as any other LIVE trade (three locks, caps,
        NO-side rule). `style` overrides the LIVE entry style (the scheduler passes "take" for Polymarket). Called by the
        release scheduler. The record carries `filled` and, on a fill, `fill_ts`."""
        self.ledger.event(ev)
        rec = {"n_candidates": n_candidates, "shortlist_ms": 0.0, "venue": market["venue"], "market_id": market["id"],
               "market_question": market["question"], "market_conf": 1.0, "materiality": 1.0,
               "p_up": 1.0 if side == "yes" else 0.0, "p_down": 0.0 if side == "yes" else 1.0,
               "action": "BUY_YES" if side == "yes" else "BUY_NO", "reason": f"release_{side}",
               "answers": {"release": {"value": value, "source": ev["source"]}},
               "mid_at_decision": bk.mid(), "mid_at_seen": self._tape_mid(market["id"], ev["seen_ts"]),
               "mid_at_published": self._tape_mid(market["id"], ev.get("published_ts"))}
        if self.tape_enabled:
            self.tape.track(market["id"], since_ts=(ev.get("published_ts") or ev["seen_ts"]) - 120)
        fill = None
        blocked, res = await self._execute(
            "live", ev, market, side, bk, self.max_trade, strength=1.0, decisive=1.0,
            mid_at_published=rec["mid_at_published"], style=style or self.styles["live"], source_tag=ev["source"])
        if blocked:
            rec["reason"] = blocked
            if blocked in COST_BLOCK_REASONS:
                rec["action"] = "PASS"
        elif res["style"] == "post":
            rec["reason"] = "post_working"
        else:
            fill = res
        fill_ts = time.time() if fill else None
        rec["total_ms"] = max(0.0, (time.time() - (ev.get("published_ts") or ev["seen_ts"])) * 1000)   # T to fill
        if fill:
            if self.verbose:
                print(f"{'':16}** RELEASE FILL {rec['action']} {fill['contracts']} @ {fill['avg_price']} "
                      f"[{ev['source']}] {ev['headline'][:60]}")
        # No await between the trade row (written inside _execute) and the decision row: a cancellation can no longer leave
        # a trade without its decision. The real order (a no-op unless the three locks are open) follows the paper records.
        out = dict(self._finish(ev, rec, fill))        # the decision row is written; timing fields are for the caller only
        out["filled"] = fill is not None
        out["fill_ts"] = fill_ts
        self.ledger.mark(ev["id"], 0, bk.best("yes"), bk.bid("yes"), bk.mid())
        if fill:
            await self._real_order(ev, market, side, fill)
        self._spawn(self._marks(ev["id"], market))
        return out

    async def _real_order(self, ev: dict, market: dict, side: str, fill: dict):
        """The paper trade is already recorded. If the user switched to real for this session, send the same buy to
        Kalshi at the paper fill's limit, sized by LIVE_MAX_ORDER_USD. Paper-only reasons are silent."""
        why_not = self.live.gate(market["venue"], market["id"], bool(ev.get("synthetic")), side=side)
        if why_not == live.NO_SIDE_REASON:
            print(f"{'':16}-- REAL ORDER SKIPPED: {live.NO_SIDE_MESSAGE}")
            return
        if why_not in ("paper_mode", "paper_only_market"):
            return
        if why_not:
            if self.verbose:
                print(f"{'':16}-- REAL ORDER SKIPPED: {why_not}")
            return
        try:
            row = await self.live.buy(ev["id"], market["id"], side, fill["limit"])
        except Exception as exc:
            errors.capture(exc, "live.buy")
            return
        self.live_orders += 1
        if self.verbose:
            print(f"{'':16}$$ REAL ORDER {side.upper()} {row.get('contracts')} @ <= {row.get('limit_price')} "
                  f"-> {row.get('status')} filled {row.get('fill_count', 0)}" +
                  (f" ({row['error']})" if row.get("error") else ""))

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
        rows = self.ledger.db.execute(f"""
            SELECT t.side, t.contracts, t.cost, t.fee,
                   (SELECT yes_bid FROM marks m WHERE m.event_id = t.event_id ORDER BY horizon_s DESC LIMIT 1),
                   (SELECT yes_ask FROM marks m WHERE m.event_id = t.event_id ORDER BY horizon_s DESC LIMIT 1)
            FROM trades t WHERE t.synthetic = 0 AND {BOOK_SQL} = 'live' AND t.opened_ts > ?""", (time.time() - 86400,)).fetchall()
        pnl = 0.0
        for side, n, cost, fee, yb, ya in rows:
            exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
            if exit_px is None and ((side == "yes" and ya is not None) or (side == "no" and yb is not None)):
                exit_px = 0.0           # the other column exists but nobody bids for the held side: worth 0. Both NULL (closed
                                        # or settled market, empty book) stays unknown and is left out
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
            line = (f"{tag}[{ev['source'][:20]:20}] src-lag {lag_s} | match {rec['shortlist_ms']:5.1f}ms "
                    f"jev {_fmt_ms(rec.get('jev_ms'))} quotes {_quotes_cell(rec)} book-wait {_fmt_ms(rec.get('book_ms'))} "
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
            tape = f"tape {'up' if self.tape.connected else 'DOWN'} {self.tape.msgs:,} quotes, {self.tape.moves} moves, {self.tape.moves_denied} denied"
        else:
            tape = "tape off (no Kalshi key)"
        xs = self.feed_stats.get("x", {})
        x = (f"x {xs.get('calls_today', 0)} calls ${xs.get('spend_today_usd', 0.0):.2f} today"
             + (" BUDGET HIT" if xs.get("budget_hit") else "") + ("" if xs.get("in_window") else " (outside window)")
             ) if self.xfeed.enabled else "x off"
        bs = self.feed_stats.get("bsky", {})
        b = (f"bsky {bs.get('mode')} {'up' if bs.get('connected') else 'DOWN'} {bs.get('new', 0)} posts"
             if self.bsky.enabled else "bsky off")
        for name in bad:
            errors.message(f"feed not answering: {name}", "feeds.dead")
        mode = "REAL" if self.live.is_live() else "paper"
        return (f"[status] mode {mode} | polls {polls} | new items {new} | decided {self.processed} | paper trades {self.trades} | shadow {self.shadow_trades} "
                f"| starter {self.starter_trades} | working {len(self.orders.working())} | real orders {self.live_orders} | queue {self.queue.qsize()} | {tape} | {x} | {b} "
                f"| shadow mode {'on' if self.shadow_enabled else 'off'}" + (f" | dead feeds: {', '.join(bad)}" if bad else ""))
