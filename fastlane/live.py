"""Real trading on Kalshi: OFF unless the user turns it on, and paper again after every restart.

Three locks, all required before a real order is sent:
1. LIVE_TRADING_ENABLED=1 in .env, plus a Kalshi API key with trading permission (KALSHI_API_KEY_ID and
   KALSHI_PRIVATE_KEY_PATH).
2. The engine is running. Every engine start writes a new session id and resets the mode to paper, so a crash and
   a Docker restart can never resume real trading on their own.
3. The user flipped the dashboard switch to REAL for this engine session, typing the confirmation phrase.

Then every order is checked against hard caps: LIVE_MAX_ORDER_USD (default 5), LIVE_MAX_DAILY_USD (default 25),
LIVE_MAX_ORDERS_PER_HOUR (default 6), one real position per market. Orders are immediate-or-cancel limit buys at the
same limit the paper fill used (best ask + 3c, never above 95c), so nothing rests on the book. Positions are held to
settlement: this module only buys. Sell or close positions on kalshi.com.

Kill switch from a terminal: python3 -m fastlane.live paper

Only Kalshi markets trade for real. Polymarket, shadow and synthetic (--inject) trades always stay on paper.
The switch flips back to paper by itself on an auth error or three failed orders in a row.
"""
import json
import math
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx

from fastlane.config import KALSHI_DEMO_BASE, RESULTS_DIR, kalshi_base_url
from fastlane.kalshi import KalshiClient
from fastlane.ratelimit import env_float

API_PREFIX = "/trade-api/v2"
ORDER_PATH = API_PREFIX + "/portfolio/events/orders"   # Create Order (V2): the only order endpoint used anywhere
BALANCE_PATH = API_PREFIX + "/portfolio/balance"
CONFIRM_PHRASE = "TRADE REAL MONEY"
MODE_FILE = RESULTS_DIR / "trading_mode.json"     # written by the dashboard API
ENGINE_FILE = RESULTS_DIR / "engine_state.json"   # written by the engine (session id, heartbeat, limits)
HEARTBEAT_STALE_S = 60
MAX_CONSECUTIVE_FAILURES = 3
_NS = uuid.UUID("5f0c6d64-3c55-4c43-9a1c-0b8f2d6e7a11")  # namespace for idempotent client order ids


def enabled_in_env() -> bool:
    return os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def allow_no_side() -> bool:
    """Real NO orders stay off until verified on Kalshi's demo environment (see README)."""
    return os.environ.get("LIVE_ALLOW_NO_SIDE", "").strip().lower() in ("1", "true", "yes", "on")


NO_SIDE_REASON = "real_no_orders_disabled"
NO_SIDE_MESSAGE = "real NO orders disabled until verified (LIVE_ALLOW_NO_SIDE=1); kept as a paper trade"


def limits() -> dict:
    return {"max_order_usd": env_float("LIVE_MAX_ORDER_USD", 5.0),
            "max_daily_usd": env_float("LIVE_MAX_DAILY_USD", 25.0),
            "max_orders_per_hour": env_float("LIVE_MAX_ORDERS_PER_HOUR", 6)}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# Paths default to the module globals at call time, so tests can point them at a temp directory.
def read_mode(path: Path | None = None) -> dict:
    return _read_json(path or MODE_FILE)


def read_engine(path: Path | None = None) -> dict:
    return _read_json(path or ENGINE_FILE)


def set_paper(reason: str, path: Path | None = None, session: str | None = None):
    _write_json(path or MODE_FILE, {"mode": "paper", "session": session, "ts": time.time(), "reason": reason})


def arm(session: str, path: Path | None = None):
    _write_json(path or MODE_FILE, {"mode": "live", "session": session, "ts": time.time(), "reason": "dashboard"})


def engine_alive(engine: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(engine.get("session")) and now - float(engine.get("heartbeat_ts") or 0) < HEARTBEAT_STALE_S


def client_order_id(event_id: str, ticker: str) -> str:
    """Same event + market always gives the same id, so a retry can never buy twice."""
    return str(uuid.uuid5(_NS, f"{event_id}|{ticker}"))


def order_body(ticker: str, side: str, contracts: int, limit_price: float, coid: str) -> dict:
    """Create Order (V2) body. V2 quotes the YES book only: `bid` buys YES at `price`; `ask` sells YES at `price`,
    which is buying NO at (1 - price). So a NO buy with limit L is an ask at 1 - L."""
    yes_price = limit_price if side == "yes" else 1 - limit_price
    return {"ticker": ticker, "client_order_id": coid, "side": "bid" if side == "yes" else "ask",
            "count": str(int(contracts)), "price": f"{yes_price:.4f}", "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "post_only": False, "reduce_only": False,
            "cancel_order_on_pause": True}


def size_order(max_usd: float, limit_price: float) -> int:
    """Whole contracts so price plus the worst-case Kalshi taker fee stays within max_usd."""
    if limit_price <= 0 or limit_price >= 1:
        return 0
    fee_per = 0.07 * limit_price * (1 - limit_price)
    return max(0, math.floor(max_usd / (limit_price + fee_per + 0.01)))


class LiveTrader:
    """Owns the real-money path. The engine asks `gate()` per trade and calls `buy()` only when it returns None."""

    def __init__(self, ledger, kalshi: KalshiClient | None = None, http: httpx.AsyncClient | None = None,
                 mode_file: Path | None = None, engine_file: Path | None = None, base_url: str | None = None):
        self.ledger = ledger
        self.kalshi = kalshi or KalshiClient()
        self.http = http
        self.mode_file, self.engine_file = mode_file or MODE_FILE, engine_file or ENGINE_FILE
        self.base_url = base_url or kalshi_base_url()
        self.session = secrets.token_hex(8)
        self.enabled = enabled_in_env()
        self.failures = 0
        self.balance_usd: float | None = None
        self.last_error: str | None = None

    # ---------- session and mode ----------
    def start(self):
        """Every engine start: new session, mode back to paper. Refuses to share a results folder with a live engine:
        two engines would fight over the switch, and the dashboard could show paper while one trades real money."""
        other = read_engine(self.engine_file)
        if engine_alive(other) and other.get("session") != self.session:
            raise RuntimeError("another fastlane engine is already running on this results folder "
                               f"(heartbeat {time.time() - float(other['heartbeat_ts']):.0f}s ago). Stop it first.")
        set_paper("engine_start", self.mode_file, self.session)
        self.heartbeat()

    def stop(self):
        """Clean shutdown: paper, and a heartbeat that reads as stopped."""
        set_paper("engine_stop", self.mode_file, self.session)
        _write_json(self.engine_file, {**read_engine(self.engine_file), "heartbeat_ts": 0})

    def heartbeat(self):
        _write_json(self.engine_file, {
            "session": self.session, "heartbeat_ts": time.time(), "live_enabled": self.enabled,
            "kalshi_configured": self.kalshi.configured, "demo": self.base_url == KALSHI_DEMO_BASE, "limits": limits(), "balance_usd": self.balance_usd,
            "spent_today_usd": round(self.ledger.live_spent_today(), 2), "last_error": self.last_error})

    def is_live(self) -> bool:
        if not (self.enabled and self.kalshi.configured):
            return False
        m = read_mode(self.mode_file)
        return m.get("mode") == "live" and m.get("session") == self.session

    def trip(self, reason: str):
        """Flip back to paper on our own. Reported so the maintainer and the user both hear about it."""
        set_paper(reason, self.mode_file, self.session)
        self.last_error = reason
        from fastlane import errors
        errors.message(f"real trading switched back to paper: {reason}", "live.trip")

    # ---------- per-trade ----------
    def gate(self, venue: str, market_id: str, synthetic: bool, now: float | None = None,
             side: str = "yes") -> str | None:
        """Why this trade must stay on paper, or None when a real order may be sent.

        Concurrency: gate() and the `sending` row that buy() writes before its first await run without yielding to
        the event loop, so two workers can never both pass for the same market or the same cap headroom. Keep it
        that way: no await may be added between them."""
        if synthetic or venue != "kalshi":
            return "paper_only_market"
        if not self.is_live():
            return "paper_mode"
        if side == "no" and not allow_no_side():
            return NO_SIDE_REASON
        lim = limits()
        if market_id in self.ledger.live_markets():
            return "live_already_in_market"
        if self.ledger.live_orders_last_hour(now) >= lim["max_orders_per_hour"]:
            return "live_hourly_order_cap"
        if self.ledger.live_spent_today(now) + lim["max_order_usd"] > lim["max_daily_usd"] + 1e-9:
            return "live_daily_cap"
        return None

    async def buy(self, event_id: str, ticker: str, side: str, limit_price: float) -> dict:
        """One immediate-or-cancel limit buy. Records the attempt before sending, the outcome after."""
        lim = limits()
        coid = client_order_id(event_id, ticker)
        if side == "no" and not allow_no_side():
            return {"event_id": event_id, "market_id": ticker, "side": side, "client_order_id": coid,
                    "status": "skipped_no_side", "error": NO_SIDE_MESSAGE}
        old = self.ledger.live_order_get(coid)
        if old:  # a reused id is never sent again and never gets a fresh timestamp: the 24 h window keeps the original
            return {**old, "status": old["status"], "reused": True}
        contracts = size_order(lim["max_order_usd"], limit_price)
        row = {"event_id": event_id, "ts": time.time(), "market_id": ticker, "side": side, "contracts": contracts,
               "limit_price": round(limit_price, 4), "client_order_id": coid, "status": "sending"}
        if contracts < 1:
            row["status"] = "skipped_too_small"
            self.ledger.live_order(**row)
            return row
        self.ledger.live_order(**row)
        body = order_body(ticker, side, contracts, limit_price, coid)
        try:
            r = await self.http.post(self.base_url + ORDER_PATH, json=body,
                                     headers=self.kalshi.sign_headers("POST", ORDER_PATH))
        except httpx.HTTPError as exc:
            # The order may or may not have reached Kalshi. Never retry; count it against the caps at full size and
            # keep the market blocked. The user checks kalshi.com.
            return self._failed(row, f"network: {type(exc).__name__}, order state unknown, check kalshi.com",
                                status="unknown")
        except Exception as exc:  # signing failure, closed client: the order may or may not have gone out
            return self._failed(row, f"{type(exc).__name__}, order state unknown, check kalshi.com", status="unknown")
        if r.status_code in (401, 403):
            out = self._failed(row, f"http_{r.status_code}")
            self.trip(f"kalshi_auth_{r.status_code}")
            return out
        if r.status_code == 409:  # same client_order_id already accepted: never retried, never doubled
            return self._done(row, "duplicate", {})
        if r.status_code == 429 or r.status_code >= 500:
            # A gateway error can arrive after Kalshi accepted the order: treat it like a network error.
            return self._failed(row, f"http_{r.status_code}, order state unknown, check kalshi.com", status="unknown")
        if r.status_code not in (200, 201):
            return self._failed(row, f"http_{r.status_code}: {r.text[:200]}")
        try:
            j = r.json()
            filled = float(j["fill_count"])
        except (ValueError, KeyError, TypeError):
            return self._failed(row, "unreadable order response, order state unknown, check kalshi.com",
                                status="unknown")
        self.failures = 0
        return self._done(row, "filled" if filled > 0 else "no_fill", j)

    def _done(self, row: dict, status: str, j: dict) -> dict:
        filled = float(j.get("fill_count") or 0)
        avg = float(j["average_fill_price"]) if j.get("average_fill_price") else None
        if avg is not None and row["side"] == "no":
            avg = round(1 - avg, 4)  # V2 reports the YES-leg price; store what one NO contract cost
        if avg is None and filled:
            avg = row["limit_price"]  # filled but no average reported: count the worst case, never zero
        fee_each = float(j.get("average_fee_paid") or 0.07 * row["limit_price"] * (1 - row["limit_price"]) if filled else 0)
        row.update(status=status, order_id=j.get("order_id"), fill_count=filled, avg_price=avg,
                   cost=round(filled * avg, 4) if avg is not None else 0.0, fee=round(filled * fee_each, 4))
        self.ledger.live_order_update(row["client_order_id"], **{k: row[k] for k in (
            "status", "order_id", "fill_count", "avg_price", "cost", "fee")})
        return row

    def _failed(self, row: dict, error: str, status: str = "error") -> dict:
        self.failures += 1
        row.update(status=status, error=error[:300])
        self.ledger.live_order_update(row["client_order_id"], status=status, error=row["error"])
        self.last_error = row["error"]
        if self.failures >= MAX_CONSECUTIVE_FAILURES:
            self.trip(f"{self.failures}_failed_orders")
        return row

    async def refresh_balance(self):
        if not (self.enabled and self.kalshi.configured and self.http):
            return
        try:
            r = await self.http.get(self.base_url + BALANCE_PATH, headers=self.kalshi.sign_headers("GET", BALANCE_PATH))
            if r.status_code == 200:
                j = r.json()
                self.balance_usd = (float(j["balance_dollars"]) if j.get("balance_dollars") is not None
                                    else float(j.get("balance", 0)) / 100)
        except (httpx.HTTPError, ValueError, KeyError):
            pass


if __name__ == "__main__":
    if sys.argv[1:] != ["paper"]:
        sys.exit("usage: python3 -m fastlane.live paper   (switch the running engine back to paper now)")
    set_paper("cli", session=read_engine().get("session"))
    print("switched to paper. No new real orders will be sent.")
