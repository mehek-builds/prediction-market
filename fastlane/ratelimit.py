"""Rate limits: per-client limits on the dashboard API and a spending budget on Jev calls.

The API limiter is in memory, per process. On Vercel each serverless instance keeps its own buckets, so the hosted
limit is best effort; it still stops one client from hammering a warm instance.
"""
import math
import os
import threading
import time
from collections import deque

from starlette.responses import JSONResponse


def env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if math.isfinite(v) else default


class TokenBucket:
    """Classic token bucket per key: `rate` tokens per second, holding at most `burst`."""

    MAX_KEYS = 10_000  # bounded memory under a flood of spoofed keys

    def __init__(self, rate_per_min: float, burst: float):
        self.rate = max(rate_per_min, 0.0) / 60.0
        self.burst = max(burst, 1.0)
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def take(self, key: str, cost: float = 1.0, now: float | None = None) -> float:
        """0.0 when allowed, else the seconds to wait before `cost` tokens are available."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._state[key] = (tokens - cost, now)
                return 0.0
            self._state[key] = (tokens, now)
            if len(self._state) > self.MAX_KEYS:
                self._state.clear()
            return (cost - tokens) / self.rate if self.rate else 3600.0


def client_key(scope: dict) -> str:
    """The caller's address. Behind Vercel the socket peer is the edge, so use the header Vercel sets."""
    if os.environ.get("VERCEL"):
        for name, value in scope.get("headers") or []:
            if name == b"x-real-ip" or name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """Pure ASGI middleware: HTTP 429 with Retry-After once a client exceeds its bucket.

    API_RATE_LIMIT_PER_MIN (default 120) and API_RATE_LIMIT_BURST (default 60). The dashboard polls two routes every
    5 s (36 a minute per open tab), so the default leaves room for a few tabs. Switching to real money gets its own,
    much tighter bucket; reading the switch state and switching back to paper never compete with it.
    """

    def __init__(self, app, per_min: float | None = None, burst: float | None = None):
        self.app = app
        self.general = TokenBucket(per_min if per_min is not None else env_float("API_RATE_LIMIT_PER_MIN", 120),
                                   burst if burst is not None else env_float("API_RATE_LIMIT_BURST", 60))
        self.control = TokenBucket(10, 5)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        tight = scope.get("method") == "POST" and scope.get("path", "") == "/control/mode"
        bucket = self.control if tight else self.general
        wait = bucket.take(client_key(scope))
        if wait:
            resp = JSONResponse({"error": "rate_limited", "retry_after_s": math.ceil(wait)}, status_code=429,
                                headers={"Retry-After": str(math.ceil(wait))})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


class JevBudget:
    """Caps Jev calls per hour and Jev spend per day, so a feed storm or a bug cannot run up the OpenRouter bill.

    JEV_MAX_CALLS_PER_HOUR (default 600) and JEV_MAX_USD_PER_DAY (default 5). Seeded from the ledger on start, so a
    restart does not reset the budget. This is a second line of defence: also set a credit limit on the key itself
    at openrouter.ai.
    """

    def __init__(self, ledger=None, max_calls_per_hour: float | None = None, max_usd_per_day: float | None = None):
        self.max_calls = (max_calls_per_hour if max_calls_per_hour is not None
                          else env_float("JEV_MAX_CALLS_PER_HOUR", 600))
        self.max_usd = max_usd_per_day if max_usd_per_day is not None else env_float("JEV_MAX_USD_PER_DAY", 5)
        self.calls: deque[float] = deque()
        self.spend: deque[tuple[float, float]] = deque()
        if ledger is not None:
            now = time.time()
            for ts, cost in ledger.db.execute(
                    "SELECT decided_ts, COALESCE(jev_cost, 0) FROM decisions WHERE jev_ms IS NOT NULL AND decided_ts > ?",
                    (now - 86400,)):
                if ts > now - 3600:
                    self.calls.append(ts)
                if cost:
                    self.spend.append((ts, float(cost)))

    def _trim(self, now: float):
        while self.calls and self.calls[0] <= now - 3600:
            self.calls.popleft()
        while self.spend and self.spend[0][0] <= now - 86400:
            self.spend.popleft()

    def spent_today(self, now: float | None = None) -> float:
        self._trim(time.time() if now is None else now)
        return sum(c for _, c in self.spend)

    def block(self, now: float | None = None) -> str | None:
        """'jev_hourly_cap' | 'jev_daily_spend_cap' | None. Checked before every Jev call."""
        now = time.time() if now is None else now
        self._trim(now)
        if self.max_calls > 0 and len(self.calls) >= self.max_calls:
            return "jev_hourly_cap"
        if self.max_usd > 0 and self.spent_today(now) >= self.max_usd:
            return "jev_daily_spend_cap"
        return None

    def record(self, cost: float | None, now: float | None = None):
        now = time.time() if now is None else now
        self.calls.append(now)
        if cost:
            self.spend.append((now, float(cost)))
