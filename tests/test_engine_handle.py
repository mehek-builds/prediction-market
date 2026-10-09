"""Engine.handle() end to end: fake Jev, fake order books, temp ledger, no network."""
import asyncio
import time

import httpx
import pytest

from fastlane import engine as engine_mod
from fastlane.books import Book
from fastlane.ledger import Ledger


class FakeJev:
    def __init__(self, answers=None, exc=None, result=None):
        self.answers, self.exc, self.result, self.calls = answers, exc, result, 0

    async def decide(self, state, questions):
        self.calls += 1
        if self.exc:
            raise self.exc
        if self.result is not None:
            return self.result
        return {"answers": self.answers(questions), "usage": {"cost": 0.001}}

    async def aclose(self):
        pass


def _answers_for(target_key_text="Avient"):
    def make(questions):
        out = {}
        for k, q in questions.items():
            hit = target_key_text in q["instructions"]
            probs = ({"decisive_yes": .8, "toward_yes": .15, "no_signal": .05} if hit
                     else {"no_signal": 1.0})
            out[k] = {"probabilities": probs}
        return out
    return make


def _event(age_s=5, **kw):
    now = time.time()
    return {"id": kw.pop("id", "ev1"), "source": "test", "headline": "Avient quarterly earnings beat",
            "summary": "", "url": "http://x", "published_ts": now - age_s, "seen_ts": now, **kw}


@pytest.fixture
def eng(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(engine_mod, "Ledger", lambda: Ledger(path))
    e = engine_mod.Engine(workers=1, verbose=False)
    e.universe = tiny_universe
    e.jev = FakeJev(_answers_for())

    async def fake_book(client, market):
        return Book("kalshi", market["id"], [(.55, 1000)], [(.47, 1000)])

    monkeypatch.setattr(engine_mod, "fetch_book", fake_book)
    return e


def _run(e, ev):
    async def go():
        try:
            return await e.handle(ev)
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    return asyncio.run(go())


def _trades(e):
    return e.ledger.db.execute("SELECT market_id, side FROM trades").fetchall()


def test_qualifying_signal_buys(eng):
    rec = _run(eng, _event())
    assert rec["action"] == "BUY_YES" and rec["reason"] == "signal_yes"
    assert _trades(eng) == [("AVNT-1", "yes")]
    row = eng.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone()
    assert row == ("BUY_YES", "signal_yes")


def test_book_failure_is_pass_no_book(eng, monkeypatch):
    async def boom(client, market):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(engine_mod, "fetch_book", boom)
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "no_book")
    assert _trades(eng) == []
    row = eng.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone()
    assert row == ("PASS", "no_book")


def test_unknown_market_is_pass(eng, monkeypatch):
    monkeypatch.setattr(engine_mod, "decide", lambda answers, keyed=None: {
        "action": "BUY_YES", "reason": "signal_yes", "key": "does-not-exist", "strength": .9,
        "p_yes_side": .9, "p_no_side": 0, "p_decisive": .5})
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "unknown_market")
    assert _trades(eng) == []


def test_jev_timeout_is_recorded_pass(eng):
    eng.jev = FakeJev(exc=httpx.ConnectTimeout("slow"))
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "jev_error")
    assert _trades(eng) == []
    row = eng.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone()
    assert row == ("PASS", "jev_error")


@pytest.mark.parametrize("err,reason", [
    (429, "jev_error_429"), ("bad_json", "jev_error_bad_json"), ({"message": "x"}, "jev_error_api"),
])
def test_jev_error_reasons(eng, err, reason):
    eng.jev = FakeJev(result={"error": err})
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", reason)
    assert _trades(eng) == []


def test_stale_news_is_blocked_without_trade(eng):
    rec = _run(eng, _event(age_s=engine_mod.MAX_NEWS_AGE_S + 60))
    assert rec["reason"] == "stale_news"
    assert rec["action"].startswith("BUY_")  # guard-blocked: intent kept, reason says why no fill
    assert rec["reason"] not in ("signal_yes", "signal_no")  # dashboard "traded" highlight keys off these
    assert eng.trades == 0
    assert _trades(eng) == []


def test_no_room_signal_is_pass_priced_in_without_trade(eng):
    for m in eng.universe.markets:
        if m["id"] == "AVNT-1":
            m["yes_ask"], m["yes_bid"] = .97, .96   # YES already near certainty; fake book (.55) would otherwise fill
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "priced_in")
    assert rec["market_id"] == "AVNT-1"           # the market is still chosen and tracked
    assert rec.get("mid_at_decision") is not None # book was fetched for marks
    assert eng.trades == 0 and _trades(eng) == []
    row = eng.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone()
    assert row == ("PASS", "priced_in")
