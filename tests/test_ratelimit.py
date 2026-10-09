"""API rate limits and the Jev call / spend budget."""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastlane import engine as engine_mod
from fastlane.ledger import Ledger
from fastlane.ratelimit import JevBudget, RateLimitMiddleware, TokenBucket, client_key

from test_engine_handle import _event, _make_engine  # noqa: E402


def test_token_bucket_refills():
    b = TokenBucket(rate_per_min=60, burst=2)
    assert b.take("a", now=0) == 0 and b.take("a", now=0) == 0
    assert b.take("a", now=0) == pytest.approx(1.0)       # 1 token a second
    assert b.take("b", now=0) == 0                          # buckets are per client
    assert b.take("a", now=1.0) == 0


def _app(per_min, burst):
    app = FastAPI()

    @app.get("/x")
    async def x():
        return {"ok": True}

    @app.get("/control/state")
    async def c():
        return {"ok": True}
    app.add_middleware(RateLimitMiddleware, per_min=per_min, burst=burst)
    return TestClient(app)


def test_middleware_returns_429_with_retry_after():
    c = _app(per_min=1, burst=2)
    assert [c.get("/x").status_code for _ in range(2)] == [200, 200]
    r = c.get("/x")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"] == "rate_limited"


def test_arming_has_its_own_tighter_bucket():
    c = _app(per_min=1000, burst=1000)
    codes = [c.post("/control/mode").status_code for _ in range(8)]   # 404 here (no route), but counted
    assert 429 in codes and codes[:5] == [404] * 5
    assert [c.get("/control/state").status_code for _ in range(20)] == [200] * 20
    assert c.get("/x").status_code == 200


def test_client_key_trusts_forwarded_header_only_on_vercel(monkeypatch):
    scope = {"client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")]}
    assert client_key(scope) == "10.0.0.1"
    monkeypatch.setenv("VERCEL", "1")
    assert client_key(scope) == "1.2.3.4"


def test_real_api_is_rate_limited(tmp_path, monkeypatch):
    from fastlane import api
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "none.db")
    mw = next(m for m in api.app.user_middleware if m.cls is RateLimitMiddleware)
    assert mw is not None


# ---------- Jev budget ----------
def test_hourly_call_cap():
    b = JevBudget(max_calls_per_hour=3, max_usd_per_day=0)
    for _ in range(3):
        assert b.block(now=100) is None
        b.record(None, now=100)
    assert b.block(now=100) == "jev_hourly_cap"
    assert b.block(now=100 + 3601) is None


def test_daily_spend_cap():
    b = JevBudget(max_calls_per_hour=0, max_usd_per_day=0.01)
    b.record(0.006, now=10)
    assert b.block(now=10) is None
    b.record(0.006, now=11)
    assert b.block(now=11) == "jev_daily_spend_cap"
    assert b.block(now=11 + 86401) is None


def test_budget_survives_restart(tmp_path):
    led = Ledger(tmp_path / "l.db")
    now = time.time()
    for i in range(4):
        led.decision(f"e{i}", jev_ms=300, jev_cost=0.01, decided_ts=now - 60)
    led.decision("old", jev_ms=300, jev_cost=5.0, decided_ts=now - 90000)   # outside both windows
    b = JevBudget(led, max_calls_per_hour=4, max_usd_per_day=1)
    assert len(b.calls) == 4 and b.spent_today() == pytest.approx(0.04)
    assert b.block() == "jev_hourly_cap"


def test_engine_skips_jev_when_capped(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.budget = JevBudget(max_calls_per_hour=1, max_usd_per_day=0)

    async def go(ev):
        try:
            return await e.handle(ev)
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
    first = asyncio.run(go(_event(id="a")))
    second = asyncio.run(go(_event(id="b")))
    asyncio.run(e.http.aclose())
    assert first["action"] == "BUY_YES" and e.jev.calls == 1
    assert second["action"] == "PASS" and second["reason"] == "jev_hourly_cap" and e.jev.calls == 1


def test_bucket_evicts_oldest_on_every_insert_and_stays_bounded():
    b = TokenBucket(rate_per_min=60, burst=5)
    b.MAX_KEYS = 50
    for i in range(500):
        b.take(f"k{i}", now=float(i))              # all allowed: growth used to be unbounded on this path
        assert len(b._state) <= 50
    assert "k499" in b._state and "k0" not in b._state     # newest kept, oldest evicted, no full clear
    b.take("tight", cost=5, now=1000.0)
    for i in range(30):
        b.take(f"x{i}", now=1000.0 + i)
    assert b.take("tight", now=1000.5) > 0                # a drained bucket survives the churn
