"""Kalshi live ticker tape over WebSocket (read-only market data, signed with the account key).

Keeps a short in-memory quote history per market so the engine can answer, for any decision:
  - what was the market quoting when the news was published / seen / decided?
  - when did it first move afterwards?
Ticks for markets the engine is tracking are also persisted to the ledger for the report.
"""
import asyncio
import json
import os
import re
import time
from collections import defaultdict, deque

import orjson
import websockets

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
HISTORY_PER_MARKET = 300
TRACK_SECONDS = 3600

# Move detector: a liquid market repricing fast is usually news breaking, seconds before any RSS feed.
MOVE_MIN = 0.05            # both bid and ask must move at least this much...
MOVE_WINDOW_S = 15         # ...within this many seconds
MOVE_MAX_SPREAD = 0.06     # and the new quote must be tight (not an empty book flickering)
MOVE_MIN_VOLUME_24H = 500  # contracts traded in the last 24h
MOVE_COOLDOWN_S = 600      # one event per Kalshi event (strike ladder) per 10 minutes
# Only categories where a sudden repricing means news broke. Crypto, weather, commodity and index ladders track a
# continuously moving underlying, so their moves are price action, not information.
MOVE_CATEGORIES = {"Politics", "Elections", "World", "Economics", "Companies", "Science and Technology", "AI",
                   "Health", "Social", "Entertainment", "Transportation", "Business"}

# Price ladders that pass the category filter but track a continuously moving underlying (AAA gas, "price on <date>"
# targets): a repricing there is price action, not news. Matched against the market title and the ticker (env MOVE_DENY_RE).
MOVE_DENY_DEFAULT = (r"\bgas prices?\b"
                     r"|\bprices?\s+(?:today|tomorrow|this week|on\s+[A-Za-z]{3,9}\.?\s+\d{1,2})\b"
                     r"|^KXAAAGAS")


def move_deny_re(env=None) -> re.Pattern:
    """Compiled MOVE_DENY_RE (case-insensitive); MOVE_DENY_DEFAULT when unset or empty. re.error -> ValueError."""
    raw = ((env if env is not None else os.environ).get("MOVE_DENY_RE") or "").strip()
    try:
        return re.compile(raw or MOVE_DENY_DEFAULT, re.I)
    except re.error as exc:
        raise ValueError(f"MOVE_DENY_RE is not a valid regex: {exc}") from exc


class KalshiTape:
    def __init__(self, ledger, universe_ids=None, market_info=None, on_move=None, sign_headers=None,
                 deny_re: re.Pattern | None = None):
        self.ledger = ledger
        self.universe_ids = universe_ids  # callable returning a set; ticks outside it are dropped
        self.hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_PER_MARKET))
        self.tracked: dict[str, float] = {}  # ticker -> track-until ts
        self.msgs = 0
        self.connected = False
        self.reconnects = 0
        self.market_info = market_info  # callable ticker -> universe market dict (or None)
        self.sign_headers = sign_headers  # callable(method, path) -> headers; None disables the stream
        self.on_move = on_move          # callable(ticker, (ts, bid, ask) before, (ts, bid, ask) now)
        self._cooldown: dict[str, float] = {}
        self.moves = 0
        self.deny_re = deny_re
        self.moves_denied = 0

    def _check_move(self, ticker: str, now: float, bid: float, ask: float):
        if not self.on_move or bid <= 0 or ask >= 1 or ask - bid > MOVE_MAX_SPREAD:
            return
        event_key = ticker.rsplit("-", 1)[0]
        if now - self._cooldown.get(event_key, 0) < MOVE_COOLDOWN_S:
            return
        before = None
        for q in self.hist[ticker]:
            if q[0] >= now - MOVE_WINDOW_S:
                break
            before = q
        if before is None:
            return
        db, da = bid - before[1], ask - before[2]
        if not ((db >= MOVE_MIN and da >= MOVE_MIN) or (db <= -MOVE_MIN and da <= -MOVE_MIN)):
            return
        info = self.market_info(ticker) if self.market_info else None
        if (not info or info.get("volume_24h", 0) < MOVE_MIN_VOLUME_24H
                or info.get("category") not in MOVE_CATEGORIES):
            return
        if self.deny_re and (self.deny_re.search(info.get("question") or "") or self.deny_re.search(ticker)):
            self.moves_denied += 1
            return
        self._cooldown[event_key] = now
        self.moves += 1
        self.on_move(ticker, info, before, (now, bid, ask))

    # ---------- queries ----------
    def quote(self, ticker: str):
        h = self.hist.get(ticker)
        return h[-1] if h else None  # (ts, bid, ask)

    def quote_at(self, ticker: str, ts: float):
        """Last quote at or before ts (None if the history does not reach back that far)."""
        h = self.hist.get(ticker)
        if not h or h[0][0] > ts:
            return None
        best = None
        for q in h:
            if q[0] > ts:
                break
            best = q
        return best

    def track(self, ticker: str, since_ts: float):
        """Persist this market's ticks for the next hour, plus whatever history we hold since `since_ts`."""
        self.tracked[ticker] = time.time() + TRACK_SECONDS
        for ts, bid, ask in list(self.hist.get(ticker, ())):
            if ts >= since_ts:
                self.ledger.tick(ticker, ts, bid, ask)

    # ---------- stream ----------
    async def run(self):
        if self.sign_headers is None:
            return
        backoff = 1
        while True:
            try:
                headers = self.sign_headers("GET", WS_PATH)
                async with websockets.connect(WS_URL, additional_headers=headers, max_size=2**22,
                                              ping_interval=20, ping_timeout=30) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}))
                    self.connected = True
                    got_msg = False
                    ids = self.universe_ids() if self.universe_ids else None
                    refreshed = time.time()
                    async for raw in ws:
                        now = time.time()
                        msg = orjson.loads(raw)
                        if msg.get("type") != "ticker":
                            continue
                        if not got_msg:
                            got_msg, backoff = True, 1  # a healthy stream: only now forget past failures
                        m = msg["msg"]
                        ticker = m.get("market_ticker")
                        if now - refreshed > 60 and self.universe_ids:
                            ids, refreshed = self.universe_ids(), now
                        if ids is not None and ticker not in ids:
                            continue
                        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
                        last = self.hist[ticker][-1] if self.hist[ticker] else None
                        if last and last[1] == bid and last[2] == ask:
                            continue  # volume-only update
                        self._check_move(ticker, now, bid, ask)
                        self.hist[ticker].append((now, bid, ask))
                        self.msgs += 1
                        until = self.tracked.get(ticker)
                        if until:
                            if now < until:
                                self.ledger.tick(ticker, now, bid, ask)
                            else:
                                del self.tracked[ticker]
                self.connected = False  # server closed the socket cleanly: back off before reconnecting
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnects += 1
                print(f"kalshi tape reconnecting in {backoff}s: {exc!r}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
