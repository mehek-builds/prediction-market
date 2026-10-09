"""Real trading: off by default, paper after every restart, capped, and only ever a single IOC buy. No network."""
import asyncio
import json
import time

import httpx
import pytest

from fastlane import engine as engine_mod
from fastlane import live
from fastlane.books import Book
from fastlane.kalshi import KalshiClient
from fastlane.ledger import Ledger

from test_engine_handle import _answers_for, _event, FakeJev  # noqa: E402  (same fake Jev and events)


class Recorder:
    """httpx MockTransport handler: records requests, answers from a queue of (status, json) or an exception."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        nxt = self.responses.pop(0) if self.responses else (201, {"order_id": "o", "fill_count": "0.00",
                                                                  "remaining_count": "0.00", "ts_ms": 1})
        if isinstance(nxt, Exception):
            raise nxt
        status, body = nxt
        return httpx.Response(status, json=body)


def _trader(tmp_path, rsa_pem, recorder, monkeypatch, enabled=True):
    if enabled:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    led = Ledger(tmp_path / "l.db")
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    t = live.LiveTrader(led, KalshiClient(key_id="kid", private_key=rsa_pem), http)
    t.start()
    return t


def _armed(t):
    live.arm(t.session)
    return t


FILLED = (201, {"order_id": "abc", "client_order_id": "x", "fill_count": "9.00", "remaining_count": "0.00",
                "average_fill_price": "0.5500", "average_fee_paid": "0.0200", "ts_ms": 1})


# ---------- pure helpers ----------
def test_order_body_yes_is_a_bid_at_the_limit():
    b = live.order_body("MK-1", "yes", 9, 0.58, "cid")
    assert b["side"] == "bid" and b["price"] == "0.5800" and b["count"] == "9"
    assert b["time_in_force"] == "immediate_or_cancel" and b["reduce_only"] is False


def test_order_body_no_is_an_ask_at_one_minus_the_limit():
    b = live.order_body("MK-1", "no", 4, 0.30, "cid")
    assert b["side"] == "ask" and b["price"] == "0.7000"   # buying NO at <= 30c == selling YES at >= 70c


def test_size_order_stays_inside_the_cap():
    n = live.size_order(5.0, 0.55)
    assert n >= 1 and n * (0.55 + 0.07 * 0.55 * 0.45) <= 5.0
    assert live.size_order(0.10, 0.55) == 0
    assert live.size_order(5.0, 1.0) == 0 and live.size_order(5.0, 0.0) == 0


def test_client_order_id_is_stable_per_event_and_market():
    a = live.client_order_id("ev1", "MK-1")
    assert a == live.client_order_id("ev1", "MK-1") != live.client_order_id("ev2", "MK-1")


# ---------- the three locks ----------
def test_paper_by_default_even_when_armed_file_exists(tmp_path, rsa_pem, monkeypatch):
    t = _trader(tmp_path, rsa_pem, Recorder(), monkeypatch, enabled=False)
    live.arm(t.session)
    assert not t.is_live()
    assert t.gate("kalshi", "MK", False) == "paper_mode"


def test_every_start_resets_to_paper(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(), monkeypatch))
    assert t.is_live()
    t.stop()
    t2 = live.LiveTrader(t.ledger, t.kalshi, t.http)
    t2.start()                       # the engine restarted
    assert not t2.is_live() and not t.is_live()
    assert live.read_mode()["mode"] == "paper" and live.read_mode()["reason"] == "engine_start"


def test_second_engine_on_the_same_results_folder_refuses_to_start(tmp_path, rsa_pem, monkeypatch):
    t = _trader(tmp_path, rsa_pem, Recorder(), monkeypatch)
    with pytest.raises(RuntimeError, match="already running"):
        live.LiveTrader(t.ledger, t.kalshi, t.http).start()
    t.stop()
    live.LiveTrader(t.ledger, t.kalshi, t.http).start()   # fine once the first one stopped


@pytest.mark.parametrize("resp", [(502, {"code": "bad_gateway"}), (429, {"code": "slow_down"}),
                                  (201, {"order_id": "o"}), (201, "not json")])
def test_unclear_outcomes_count_as_unknown(tmp_path, rsa_pem, monkeypatch, resp):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(resp), monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert row["status"] == "unknown" and "MK-1" in t.ledger.live_markets()
    assert t.ledger.live_spent_today() > 0


def test_filled_without_average_price_counts_at_the_limit(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder((201, {"order_id": "o", "fill_count": "3.00"})), monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert row["status"] == "filled" and row["cost"] == pytest.approx(1.65) and row["fee"] > 0


def test_arming_another_session_does_nothing(tmp_path, rsa_pem, monkeypatch):
    t = _trader(tmp_path, rsa_pem, Recorder(), monkeypatch)
    live.arm("some-old-session")
    assert not t.is_live()


def test_no_kalshi_key_means_no_real_trading(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    t = live.LiveTrader(Ledger(tmp_path / "l.db"), KalshiClient(key_id="", private_key=""), None)
    t.start()
    live.arm(t.session)
    assert not t.is_live()


@pytest.mark.parametrize("venue,synthetic", [("polymarket", False), ("kalshi", True)])
def test_polymarket_and_synthetic_stay_paper(tmp_path, rsa_pem, monkeypatch, venue, synthetic):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(), monkeypatch))
    assert t.gate(venue, "MK", synthetic) == "paper_only_market"


# ---------- caps ----------
def test_daily_cap_and_hourly_cap_and_one_position_per_market(tmp_path, rsa_pem, monkeypatch):
    monkeypatch.setenv("LIVE_MAX_ORDER_USD", "5")
    monkeypatch.setenv("LIVE_MAX_DAILY_USD", "12")
    monkeypatch.setenv("LIVE_MAX_ORDERS_PER_HOUR", "3")
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(FILLED, FILLED, FILLED), monkeypatch))
    asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert t.gate("kalshi", "MK-1", False) == "live_already_in_market"
    assert t.gate("kalshi", "MK-2", False) is None
    asyncio.run(t.buy("e2", "MK-2", "yes", .55))
    assert t.ledger.live_spent_today() == pytest.approx(2 * (9 * .55 + 9 * .02))
    assert t.gate("kalshi", "MK-3", False) == "live_daily_cap"         # 10.26 spent + 5 > 12
    monkeypatch.setenv("LIVE_MAX_DAILY_USD", "100")
    asyncio.run(t.buy("e3", "MK-3", "yes", .55))
    assert t.gate("kalshi", "MK-4", False) == "live_hourly_order_cap"


def test_unknown_outcome_counts_at_full_size(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(httpx.ReadTimeout("slow")), monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert row["status"] == "unknown" and "check kalshi.com" in row["error"]
    n = row["contracts"]
    assert t.ledger.live_spent_today() == pytest.approx(n * (.55 + .07 * .55 * .45))
    assert "MK-1" in t.ledger.live_markets()          # never retried on the same market


# ---------- the order itself ----------
def test_buy_sends_one_signed_ioc_order_and_records_the_fill(tmp_path, rsa_pem, monkeypatch):
    rec = Recorder(FILLED)
    t = _armed(_trader(tmp_path, rsa_pem, rec, monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "yes", .58))
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/trade-api/v2/portfolio/events/orders"
    assert {"kalshi-access-key", "kalshi-access-signature", "kalshi-access-timestamp"} <= set(req.headers)
    body = json.loads(req.content)
    assert body["time_in_force"] == "immediate_or_cancel" and body["side"] == "bid"
    assert body["client_order_id"] == live.client_order_id("e1", "MK-1")
    assert row["status"] == "filled" and row["fill_count"] == 9 and row["avg_price"] == .55
    db = t.ledger.db.execute("SELECT status, order_id, cost, fee FROM live_orders").fetchone()
    assert db == ("filled", "abc", pytest.approx(4.95), pytest.approx(.18))


def test_no_fill_price_is_stored_as_the_no_contract_price(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(FILLED), monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "no", .48))
    assert row["avg_price"] == pytest.approx(.45)       # YES-leg 0.55 reported, NO cost 0.45


def test_auth_error_trips_back_to_paper(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder((401, {"code": "unauthorized"})), monkeypatch))
    asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert not t.is_live() and live.read_mode()["reason"] == "kalshi_auth_401"


def test_three_failures_in_a_row_trip_back_to_paper(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(*[(400, {"code": "bad"})] * 3), monkeypatch))
    for i in range(2):
        asyncio.run(t.buy(f"e{i}", f"MK-{i}", "yes", .55))
        assert t.is_live()
    asyncio.run(t.buy("e2", "MK-2", "yes", .55))
    assert not t.is_live() and live.read_mode()["reason"] == "3_failed_orders"


def test_conflict_is_a_duplicate_not_a_second_order(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder((409, {"code": "duplicate"})), monkeypatch))
    row = asyncio.run(t.buy("e1", "MK-1", "yes", .55))
    assert row["status"] == "duplicate" and "MK-1" in t.ledger.live_markets()


# ---------- engine integration ----------
def _engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, recorder, enabled=True):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    if enabled:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setattr(engine_mod, "Ledger", lambda: Ledger(tmp_path / "ledger.db"))
    monkeypatch.setattr(engine_mod, "KalshiClient", lambda: KalshiClient(key_id="kid", private_key=rsa_pem))
    e = engine_mod.Engine(workers=1, verbose=False)
    e.tape_enabled = False
    e.universe = tiny_universe
    e.jev = FakeJev(_answers_for())
    e.live.http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    e.live.start()

    async def fake_book(client, market):
        return Book("kalshi", market["id"], [(.55, 1000)], [(.47, 1000)])
    monkeypatch.setattr(engine_mod, "fetch_book", fake_book)
    return e


def _handle(e, ev):
    async def go():
        try:
            out = await e.handle(ev)
            await e.drain_shadow()
            return out
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    return asyncio.run(go())


def test_engine_on_paper_never_sends_an_order(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec = Recorder()
    e = _engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec)
    out = _handle(e, _event())
    assert out["action"] == "BUY_YES"
    assert rec.requests == [] and e.ledger.db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_engine_armed_sends_exactly_one_order_at_the_paper_limit(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec = Recorder(FILLED)
    e = _engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec)
    live.arm(e.live.session)
    _handle(e, _event())
    assert len(rec.requests) == 1
    body = json.loads(rec.requests[0].content)
    assert body["ticker"] == "AVNT-1" and body["price"] == "0.5800"   # best ask 0.55 + 3c slippage, as on paper
    assert e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] >= 1   # the paper book still records it


def test_engine_synthetic_events_never_trade_real(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec = Recorder(FILLED)
    e = _engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec)
    live.arm(e.live.session)
    _handle(e, _event(synthetic=True))
    assert rec.requests == []


def test_kill_switch_cli(tmp_path, rsa_pem, monkeypatch):
    t = _armed(_trader(tmp_path, rsa_pem, Recorder(), monkeypatch))
    t.heartbeat()
    live.set_paper("cli", session=live.read_engine()["session"])
    assert not t.is_live()


def test_heartbeat_reports_state_without_secrets(tmp_path, rsa_pem, monkeypatch):
    t = _trader(tmp_path, rsa_pem, Recorder(), monkeypatch)
    t.heartbeat()
    raw = live.ENGINE_FILE.read_text()
    eng = json.loads(raw)
    assert eng["session"] == t.session and eng["live_enabled"] and eng["kalshi_configured"]
    assert live.engine_alive(eng) and not live.engine_alive(eng, now=time.time() + 120)
    assert "PRIVATE" not in raw and "kid" not in raw
