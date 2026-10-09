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
        self.last_questions = None

    async def decide(self, state, questions):
        self.calls += 1
        self.last_questions = questions
        if self.exc:
            raise self.exc
        if self.result is not None:
            return self.result
        return {"answers": self.answers(questions), "usage": {"cost": 0.001}}

    async def aclose(self):
        pass


LEAN = {"toward_yes": .9, "decisive_yes": .1}                       # real lean_not_decisive, shadow BUY_YES (0.85+)
WEAK = {"decisive_yes": .3, "toward_yes": .35, "no_signal": .35}    # real weak_signal, shadow BUY_YES (0.60-0.70)


def _answers_for(target_key_text="Avient", probs=None):
    def make(questions):
        out = {}
        for k, q in questions.items():
            hit = target_key_text in q["instructions"]
            probs_k = (probs or {"decisive_yes": .8, "toward_yes": .15, "no_signal": .05}) if hit else {"no_signal": 1.0}
            probs_ = probs_k
            out[k] = {"probabilities": probs_}
        return out
    return make


def _event(age_s=5, **kw):
    now = time.time()
    return {"id": kw.pop("id", "ev1"), "source": "test", "headline": "Avient quarterly earnings beat",
            "summary": "", "url": "http://x", "published_ts": now - age_s, "seen_ts": now, **kw}


def _make_engine(monkeypatch, tmp_path, tiny_universe, book=None):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(engine_mod, "Ledger", lambda: Ledger(path))
    e = engine_mod.Engine(workers=1, verbose=False)
    e.universe = tiny_universe
    e.jev = FakeJev(_answers_for())

    make_book = book or (lambda mid: Book("kalshi", mid, [(.55, 1000)], [(.47, 1000)]))

    async def fake_book(client, market):
        return make_book(market["id"])

    monkeypatch.setattr(engine_mod, "fetch_book", fake_book)
    return e


@pytest.fixture
def eng(monkeypatch, tmp_path, tiny_universe):
    return _make_engine(monkeypatch, tmp_path, tiny_universe)


def _run(e, ev):
    async def go():
        try:
            out = await e.handle(ev)
            await e.drain_shadow()   # the shadow pass is a background task: wait for it so tests stay deterministic
            return out
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    return asyncio.run(go())


def _trades(e):
    return e.ledger.db.execute("SELECT market_id, side, shadow FROM trades").fetchall()


def test_qualifying_signal_buys(eng):
    rec = _run(eng, _event())
    assert rec["action"] == "BUY_YES" and rec["reason"] == "signal_yes"
    assert _trades(eng) == [("AVNT-1", "yes", 0)]
    tr = eng.ledger.db.execute("SELECT signal_strength, signal_decisive FROM trades").fetchone()
    assert tr[0] == pytest.approx(.95) and tr[1] == pytest.approx(.8)
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
    monkeypatch.setattr(engine_mod, "decide", lambda answers, keyed=None, **kw: {
        "action": "BUY_YES", "reason": "signal_yes", "key": "does-not-exist", "strength": .9,
        "p_yes_side": .9, "p_no_side": 0, "p_decisive": .5})
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "unknown_market")
    assert rec["shadow_reason"] == "real_signalled"   # real BUY intent: shadow never runs
    assert _trades(eng) == []
    assert _shadow_row(eng) == (None, "real_signalled", None)


def test_real_buy_with_failed_book_never_reaches_shadow(eng, monkeypatch):
    calls = []

    async def flaky(client, market):
        calls.append(market["id"])
        raise httpx.ConnectError("down")

    monkeypatch.setattr(engine_mod, "fetch_book", flaky)
    rec = _run(eng, _event())                        # decisive 0.95 signal, real book unavailable
    assert (rec["action"], rec["reason"]) == ("PASS", "no_book")
    assert (rec["shadow_action"], rec["shadow_reason"]) == (None, "real_signalled")
    assert _trades(eng) == [] and eng.shadow_trades == 0
    assert _shadow_row(eng) == (None, "real_signalled", None)
    assert calls == ["AVNT-1"]                       # no second fetch spent on a shadow retry


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


def _shadow_row(e):
    return e.ledger.db.execute("SELECT shadow_action, shadow_reason, shadow_market_id FROM decisions WHERE event_id='ev1'").fetchone()


def test_lean_only_is_real_pass_and_shadow_trade(eng):
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "lean_not_decisive")
    assert (rec["shadow_action"], rec["shadow_reason"], rec["shadow_market_id"]) == ("BUY_YES", "signal_yes", "AVNT-1")
    assert _trades(eng) == [("AVNT-1", "yes", 1)] and eng.trades == 0 and eng.shadow_trades == 1
    assert _shadow_row(eng) == ("BUY_YES", "signal_yes", "AVNT-1")
    row = eng.ledger.db.execute("SELECT signal_strength, signal_decisive FROM trades").fetchone()
    assert row[0] == pytest.approx(1.0) and row[1] == pytest.approx(.1)
    keys = {r[0] for r in eng.ledger.db.execute("SELECT event_id FROM marks WHERE horizon_s = 0")}
    assert keys == {"ev1", "shadow:ev1"}          # real PASS tracking + shadow trade, no collision
    assert eng.ledger.traded_markets() == set() and eng.ledger.spent_today() == 0


def test_weak_signal_shadow_trade_at_default_threshold(eng):
    eng.jev = FakeJev(_answers_for(probs=WEAK))
    rec = _run(eng, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "weak_signal")
    assert rec["shadow_reason"] == "signal_yes" and _trades(eng) == [("AVNT-1", "yes", 1)]


def test_shadow_marks_are_scheduled(eng):
    eng.jev = FakeJev(_answers_for(probs=LEAN))

    async def go():
        await eng.handle(_event())
        await eng.drain_shadow()
        names = [t.get_coro().__qualname__ for t in eng._bg]
        assert names.count("Engine._marks") == 2   # one for the real tracked market, one for the shadow trade
        for t in list(eng._bg):
            t.cancel()
        await asyncio.gather(*list(eng._bg), return_exceptions=True)
        await eng.http.aclose()
    asyncio.run(go())


def test_shadow_not_recorded_when_real_trades(eng):
    rec = _run(eng, _event())
    assert rec["action"] == "BUY_YES" and _trades(eng) == [("AVNT-1", "yes", 0)]
    assert rec["shadow_action"] is None and rec["shadow_reason"] == "real_signalled"
    assert eng.shadow_trades == 0 and _shadow_row(eng) == (None, "real_signalled", None)


def test_shadow_disabled_by_env(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("SHADOW_ENABLED", "false")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(e, _event())
    assert e.shadow_enabled is False and "shadow_action" not in rec
    assert _trades(e) == [] and _shadow_row(e) == (None, None, None)


def test_shadow_thresholds_from_env(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("SHADOW_SIGNAL_THRESHOLD", "0.99")   # stricter than the lean: shadow passes too
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.jev = FakeJev(_answers_for(probs={"toward_yes": .9, "decisive_yes": .05, "no_signal": .05}))
    rec = _run(e, _event())
    assert (rec["shadow_action"], rec["shadow_reason"]) == ("PASS", "weak_signal") and _trades(e) == []


def test_shadow_ignores_real_bankroll_and_halt(eng):
    eng.bankroll, eng.halt_loss = 0.0, -1.0                  # any real trade would be bankroll_exhausted / halted
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(eng, _event())
    assert rec["shadow_reason"] == "signal_yes" and _trades(eng) == [("AVNT-1", "yes", 1)]


def test_real_ignores_shadow_position_and_pnl(eng):
    eng.ledger.trade(event_id="ev0", opened_ts=time.time(), venue="kalshi", market_id="AVNT-1", market_question="Q",
                     side="yes", contracts=100, avg_price=.9, cost=90.0, fee=.5, best_ask=.9, synthetic=0, shadow=1)
    eng.ledger.mark("shadow:ev0", 5, .2, .1, .15)            # shadow book deep underwater
    assert eng._today_pnl() == 0.0
    rec = _run(eng, _event())                                # decisive answers: real buys despite the shadow position
    assert (rec["action"], rec["reason"]) == ("BUY_YES", "signal_yes")
    assert ("AVNT-1", "yes", 0) in _trades(eng)


def test_shadow_one_position_per_market(eng):
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    _run(eng, _event())
    eng.http = httpx.AsyncClient()                            # _run closed it
    rec2 = _run(eng, _event(id="ev2"))
    assert (rec2["shadow_action"], rec2["shadow_reason"]) == ("BUY_YES", "already_in_market")
    assert len(_trades(eng)) == 1


def test_shadow_respects_stale_news(eng):
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(eng, _event(age_s=engine_mod.MAX_NEWS_AGE_S + 60))
    assert (rec["shadow_action"], rec["shadow_reason"]) == ("BUY_YES", "stale_news") and _trades(eng) == []


def test_shadow_skips_synthetic_events(eng):
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(eng, _event(synthetic=True))
    assert rec["action"] == "PASS" and "shadow_action" not in rec and _trades(eng) == []


def test_shadow_book_failure_is_no_book(eng, monkeypatch):
    eng.jev = FakeJev(_answers_for(probs=LEAN))

    async def boom(client, market):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(engine_mod, "fetch_book", boom)
    rec = _run(eng, _event())
    assert rec["reason"] == "lean_not_decisive"   # real PASS keeps its own reason
    assert (rec["shadow_action"], rec["shadow_reason"], rec["shadow_market_id"]) == ("PASS", "no_book", "AVNT-1")
    assert _trades(eng) == []


def test_real_too_expensive_is_pass_and_shadow_skipped(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe,
                     book=lambda mid: Book("kalshi", mid, [(.55, 1000)], [(.50, 1000)]))   # yes bid .50: spread 5c
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "too_expensive")
    assert rec["market_id"] == "AVNT-1" and rec.get("mid_at_decision") is not None     # still tracked
    assert _trades(e) == [] and e.trades == 0
    assert rec["shadow_reason"] == "real_signalled"
    row = e.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone()
    assert row == ("PASS", "too_expensive")


def test_too_expensive_env_override(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("MAX_SPREAD_CENTS", "6")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe,
                     book=lambda mid: Book("kalshi", mid, [(.55, 1000)], [(.50, 1000)]))
    rec = _run(e, _event())
    assert rec["reason"] == "signal_yes" and len(_trades(e)) == 1


def test_shadow_goes_through_cost_filter(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe,
                     book=lambda mid: Book("kalshi", mid, [(.55, 1000)], [(.50, 1000)]))
    e.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "lean_not_decisive")
    assert (rec["shadow_action"], rec["shadow_reason"]) == ("PASS", "too_expensive") and _trades(e) == []


def test_two_markets_real_passes_on_a_shadow_buys_b(monkeypatch, tmp_path, tiny_universe):
    a = {"venue": "kalshi", "id": "AVNT-2", "question": "Avient quarterly earnings beat estimates", "category": "Companies",
         "yes_ask": .31, "yes_bid": .29, "volume_24h": 200}
    tiny_universe._set(tiny_universe.markets + [a])
    fetched = []

    def book(mid):
        fetched.append(mid)
        if mid == "AVNT-2":
            return Book("kalshi", mid, [(.31, 1000)], [(.71, 1000)])    # roomy: ask .31, yes bid .29
        return Book("kalshi", mid, [(.55, 1000)], [(.47, 1000)])

    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=book)

    def answers(questions):
        out = {}
        for k, q in questions.items():
            if "Avient" not in q["instructions"]:
                out[k] = {"probabilities": {"no_signal": 1.0}}
            elif "estimates" in q["instructions"]:   # market B: weaker, but more room
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .35, "no_signal": .35}}
            else:                                    # market A (AVNT-1): strongest, real still passes (weak_signal)
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .4, "no_signal": .3}}
        return out

    e.jev = FakeJev(answers)
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "weak_signal") and rec["market_id"] == "AVNT-1"
    assert (rec["shadow_action"], rec["shadow_reason"], rec["shadow_market_id"]) == ("BUY_YES", "signal_yes", "AVNT-2")
    assert _trades(e) == [("AVNT-2", "yes", 1)]
    assert _shadow_row(e) == ("BUY_YES", "signal_yes", "AVNT-2")
    keys = {r[0] for r in e.ledger.db.execute("SELECT event_id FROM marks WHERE horizon_s = 0")}
    assert keys == {"ev1", "shadow:ev1"}
    assert sorted(fetched) == ["AVNT-1", "AVNT-2"]   # B's book came from the prefetch: no extra fetch


def test_shadow_runs_in_background_and_is_cancellable(eng, monkeypatch):
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    gate = asyncio.Event()

    async def go():
        # the shadow stage waits on a gate, so handle() must return first
        orig = eng._shadow_decide

        async def held(*a, **kw):
            await gate.wait()
            return await orig(*a, **kw)

        monkeypatch.setattr(eng, "_shadow_decide", held)
        rec = await eng.handle(_event())
        assert "shadow_action" not in rec and _trades(eng) == []        # handle() returned; shadow still pending
        assert len(eng._shadow_tasks) == 1
        await eng.stop()                                                # cancels the pending shadow task
        assert not eng._shadow_tasks and _trades(eng) == []
    asyncio.run(go())


def test_shadow_exception_is_logged_not_raised(eng, monkeypatch, capsys):
    eng.jev = FakeJev(_answers_for(probs=LEAN))

    real_decide = engine_mod.decide

    def flaky(answers, keyed=None, **kw):
        if kw:                      # only the shadow call passes thresholds
            raise RuntimeError("shadow bug")
        return real_decide(answers, keyed)

    monkeypatch.setattr(engine_mod, "decide", flaky)
    rec = _run(eng, _event())
    assert rec["reason"] == "lean_not_decisive"
    out = capsys.readouterr().out
    assert "shadow error" in out and "handle error" not in out
    assert _trades(eng) == []


def test_status_mentions_shadow(eng):
    assert "shadow 0" in eng.status()


def test_real_buy_blocked_by_freshness_guard_is_real_signalled(eng):
    rec = _run(eng, _event(age_s=engine_mod.MAX_NEWS_AGE_S + 60))
    assert rec["reason"] == "stale_news" and rec["action"].startswith("BUY_")
    assert _shadow_row(eng) == (None, "real_signalled", None) and _trades(eng) == []


def test_real_buy_blocked_by_already_in_market_is_real_signalled(eng):
    _run(eng, _event())
    eng.http = httpx.AsyncClient()
    rec2 = _run(eng, _event(id="ev2"))
    assert rec2["reason"] == "already_in_market"
    assert eng.ledger.db.execute("SELECT shadow_action, shadow_reason FROM decisions WHERE event_id='ev2'").fetchone() \
        == (None, "real_signalled")
    assert _trades(eng) == [("AVNT-1", "yes", 0)]


def test_real_buy_blocked_by_bankroll_is_real_signalled(eng):
    eng.bankroll = 0
    rec = _run(eng, _event())
    assert rec["reason"] == "bankroll_exhausted"
    assert _shadow_row(eng) == (None, "real_signalled", None) and _trades(eng) == []


def test_concurrent_same_market_events_make_one_shadow_trade(eng, monkeypatch):
    eng.jev = FakeJev(_answers_for(probs=LEAN))

    async def slow_book(client, market):
        await asyncio.sleep(0.02)
        return Book("kalshi", market["id"], [(.55, 1000)], [(.47, 1000)])

    monkeypatch.setattr(engine_mod, "fetch_book", slow_book)

    async def go():
        try:
            await asyncio.gather(*[eng.handle(_event(id=f"c{i}")) for i in range(6)])
            await eng.drain_shadow()
        finally:
            for t in list(eng._bg):
                t.cancel()
            await asyncio.gather(*list(eng._bg), return_exceptions=True)
            await eng.http.aclose()
    asyncio.run(go())
    assert _trades(eng) == [("AVNT-1", "yes", 1)]
    reasons = sorted(r[0] for r in eng.ledger.db.execute("SELECT shadow_reason FROM decisions"))
    assert reasons == ["already_in_market"] * 5 + ["signal_yes"]


def test_status_mentions_fast_sources(eng):
    eng.tape_enabled = True            # _make_engine runs with the tape off; the denied count only shows with it on
    s = eng.status()
    assert "x off" in s and "bsky idle DOWN 0 posts" in s and "0 denied" in s


def test_status_x_segment_when_enabled(eng):
    eng.xfeed.enabled = True
    eng.feed_stats["x"].update(calls_today=3, spend_today_usd=0.165, budget_hit=True, in_window=False)
    s = eng.status()
    assert "x 3 calls $0.17 today BUDGET HIT (outside window)" in s or "x 3 calls $0.16 today BUDGET HIT (outside window)" in s


def test_move_event_excludes_same_series_not_others(monkeypatch, tmp_path, tiny_universe):
    extra = [{"venue": "kalshi", "id": "KXFEDDECISION-25OCT29-C25", "question": "Fed decision in October: Cut 25 bps", "category": "Economics", "yes_ask": .6, "yes_bid": .58, "volume_24h": 900},
             {"venue": "kalshi", "id": "KXFEDDECISION-25DEC10-C25", "question": "Fed decision in December: Cut 25 bps", "category": "Economics", "yes_ask": .5, "yes_bid": .48, "volume_24h": 900},
             {"venue": "kalshi", "id": "KXFEDCHAIR-26MAY-POW", "question": "Fed decision: next Fed chair confirmed", "category": "Politics", "yes_ask": .3, "yes_bid": .28, "volume_24h": 900}]
    tiny_universe._set(tiny_universe.markets + extra)
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.jev = FakeJev(lambda qs: {k: {"probabilities": {"no_signal": 1.0}} for k in qs})
    ev = {"id": "move-KXFEDDECISION-25OCT29-C25-1", "source": "kalshi_move", "summary": "", "url": "", "published_ts": time.time(),
          "seen_ts": time.time(), "headline": 'Prediction market "Fed decision in October: Cut 25 bps" just repriced from 50% to 60% within 5 seconds',
          "exclude_series": "KXFEDDECISION"}
    _run(e, ev)
    asked = " ".join(q["instructions"] for q in e.jev.last_questions.values())
    assert "Cut 25 bps" not in asked and "next Fed chair confirmed" in asked


def test_on_move_sets_exclude_series_not_event(eng):
    eng._on_move("KXFEDDECISION-25OCT29-C25", {"question": "Fed decision in October: Cut 25 bps"},
                 (time.time() - 5, .50, .52), (time.time(), .60, .62))
    ev = eng.queue.get_nowait()
    assert ev["exclude_series"] == "KXFEDDECISION" and "exclude_event" not in ev


def test_polymarket_candidates_never_excluded_by_series(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.jev = FakeJev(_answers_for(target_key_text="Bitcoin"))
    ev = _event(headline="Bitcoin ladder moonshot", exclude_series="9001")
    _run(e, ev)
    assert any("moonshot" in q["instructions"] for q in e.jev.last_questions.values())


def test_no_exit_liquidity_is_pass_real_and_shadow(monkeypatch, tmp_path, tiny_universe):
    nobid = lambda mid: Book("kalshi", mid, [(.55, 1000)], [])             # no NO asks: no YES bid
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=nobid)
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "no_exit_liquidity") and _trades(e) == []
    assert rec["shadow_reason"] == "real_signalled"
    (tmp_path / "b").mkdir()
    e2 = _make_engine(monkeypatch, tmp_path / "b", tiny_universe, book=nobid)
    e2.jev = FakeJev(_answers_for(probs=LEAN))
    rec2 = _run(e2, _event())
    assert (rec2["shadow_action"], rec2["shadow_reason"]) == ("PASS", "no_exit_liquidity") and _trades(e2) == []


def test_one_cent_no_with_no_bid_incident(monkeypatch, tmp_path, tiny_universe):
    """The live incident: 20,000 NO contracts at 1c where the NO side had no bid. Must never fill."""
    nobid_no = lambda mid: Book("kalshi", mid, [], [(.01, 20000)])
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=nobid_no)
    e.jev = FakeJev(_answers_for(probs={"decisive_no": .8, "toward_no": .15, "no_signal": .05}))
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "no_exit_liquidity")
    assert _trades(e) == []


def test_longshot_is_pass(monkeypatch, tmp_path, tiny_universe):
    cheap = lambda mid: Book("kalshi", mid, [(.02, 100000)], [(.97, 1000)])  # yes ask 2c, yes bid 3c
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=cheap)
    rec = _run(e, _event())
    assert (rec["action"], rec["reason"]) == ("PASS", "longshot") and _trades(e) == []
    monkeypatch.setenv("MIN_ENTRY_PRICE", "0.02")
    (tmp_path / "b").mkdir()
    e2 = _make_engine(monkeypatch, tmp_path / "b", tiny_universe, book=cheap)
    assert _run(e2, _event())["reason"] != "longshot"


def _two_market_engine(monkeypatch, tmp_path, tiny_universe):
    a = {"venue": "kalshi", "id": "AVNT-2", "question": "Avient quarterly earnings beat estimates", "category": "Companies",
         "yes_ask": .31, "yes_bid": .29, "volume_24h": 200}
    tiny_universe._set(tiny_universe.markets + [a])

    def book(mid):
        return Book("kalshi", mid, [(.31, 1000)], [(.71, 1000)]) if mid == "AVNT-2" else Book("kalshi", mid, [(.55, 1000)], [(.47, 1000)])
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=book)
    e.verbose = True

    def answers(questions):
        out = {}
        for k, q in questions.items():
            if "Avient" not in q["instructions"]:
                out[k] = {"probabilities": {"no_signal": 1.0}}
            elif "estimates" in q["instructions"]:
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .35, "no_signal": .35}}
            else:
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .4, "no_signal": .3}}
        return out
    e.jev = FakeJev(answers)
    return e


def test_shadow_fill_line_names_the_shadow_market(monkeypatch, tmp_path, tiny_universe, capsys):
    e = _two_market_engine(monkeypatch, tmp_path, tiny_universe)
    rec = _run(e, _event())
    assert rec["market_id"] == "AVNT-1" and rec["shadow_market_id"] == "AVNT-2"
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "shadow market ->" in l)
    assert "Avient quarterly earnings beat estimates" in line and "kalshi" in line and "differs from the real decision" in line


def test_shadow_fill_line_has_no_suffix_when_same_market(eng, capsys):
    eng.verbose = True
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    rec = _run(eng, _event())
    assert rec["shadow_market_id"] == "AVNT-1"
    line = next(l for l in capsys.readouterr().out.splitlines() if "shadow market ->" in l)
    assert "Avient quarterly earnings beat" in line and "differs" not in line


def test_finish_line_fits_long_source(eng, capsys):
    eng.verbose = True
    _run(eng, _event(source="bsky:washingtonpost.com"))
    assert "[bsky:washingtonpost.]" in capsys.readouterr().out


# ---------- v0.4.0: runtime shadow toggle ----------
def test_shadow_toggle_at_runtime_without_restart(eng):
    e = eng
    e.jev = FakeJev(_answers_for(probs=LEAN))
    assert e.shadow_enabled is True
    e.ledger.set_setting("shadow_enabled", "0"); e._shadow_checked_ts = 0.0     # force the 2 s cache to expire
    rec = _run(e, _event(id="ev-off"))
    assert e.shadow_enabled is False and "shadow_action" not in rec and _trades(e) == []
    e.ledger.set_setting("shadow_enabled", "1"); e._shadow_checked_ts = 0.0
    rec = _run(e, _event(id="ev-on"))
    assert rec["shadow_reason"] == "signal_yes" and _trades(e) == [("AVNT-1", "yes", 1)]


def test_shadow_off_records_no_shadow_columns_or_real_signalled_row(eng):
    e = eng
    e.ledger.set_setting("shadow_enabled", "0"); e._shadow_checked_ts = 0.0
    rec = _run(e, _event())                       # default answers pass the real rule: a real fill, no shadow row
    assert rec["reason"] == "signal_yes" and _trades(e) == [("AVNT-1", "yes", 0)]
    assert e.ledger.db.execute("SELECT shadow_action, shadow_reason, shadow_market_id FROM decisions").fetchone() == (None, None, None)


def test_shadow_setting_row_beats_env(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("SHADOW_ENABLED", "false")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    assert e.shadow_enabled is False
    e.ledger.set_setting("shadow_enabled", "1"); e._shadow_checked_ts = 0.0
    assert e.shadow_enabled is True


def test_shadow_setting_is_cached(eng):
    assert eng.shadow_enabled is True
    eng.ledger.set_setting("shadow_enabled", "0")
    assert eng.shadow_enabled is True          # within the 2 s window the cached value is served
    eng._shadow_checked_ts = 0.0
    assert eng.shadow_enabled is False


def test_shadow_setting_cache_expires_after_window(eng, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t[0])
    eng._shadow_checked_ts = 0.0
    assert eng.shadow_enabled is True
    eng.ledger.set_setting("shadow_enabled", "0")
    t[0] += 1.9
    assert eng.shadow_enabled is True
    t[0] += 0.2                                # 2.1 s after the read
    assert eng.shadow_enabled is False


def test_real_decision_is_recorded_before_any_setting_read(eng, monkeypatch):
    """The real path never waits on the shadow switch: the decision row exists when the setting is first read."""
    seen = []
    real = engine_mod.shadow_enabled_from

    def spy(db):
        seen.append(db.execute("SELECT action, reason FROM decisions WHERE event_id = 'ev1'").fetchone())
        return real(db)

    monkeypatch.setattr(engine_mod, "shadow_enabled_from", spy)
    eng._shadow_checked_ts = 0.0
    eng.jev = FakeJev(_answers_for(probs=LEAN))
    _run(eng, _event())
    assert seen and all(s is not None for s in seen)


def test_real_path_unaffected_when_setting_read_fails(eng, monkeypatch):
    """Even a broken settings read (here: raising) cannot undo a real fill that is already recorded."""
    def boom(db):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(engine_mod, "shadow_enabled_from", boom)
    eng._shadow_checked_ts = 0.0
    try:
        _run(eng, _event())
    except RuntimeError:
        pass
    assert ("AVNT-1", "yes", 0) in _trades(eng)


def test_status_line_reports_shadow_mode(eng):
    assert "shadow mode on" in eng.status()
    eng.ledger.set_setting("shadow_enabled", "0"); eng._shadow_checked_ts = 0.0
    assert "shadow mode off" in eng.status()


def test_start_writes_x_disabled_status_when_x_is_off(eng, monkeypatch):
    """Leftover fix: with no XAI key the heartbeat row says {"enabled": false}, never stale from an older run."""
    eng.ledger.feed_status_set("x", True, {"enabled": True, "calls_today": 9})    # left over from an earlier run
    assert eng.xfeed.enabled is False

    async def noload(client, force=False):
        return "cache"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(eng.universe, "load", noload)
    monkeypatch.setattr(eng, "_keepwarm_once", noop)
    monkeypatch.setattr(eng, "_index_kalshi", lambda: None)
    monkeypatch.setattr(eng.hub, "tasks", lambda: [])
    monkeypatch.setattr(eng.bsky, "run", noop)
    monkeypatch.setattr(eng, "_worker", noop)
    monkeypatch.setattr(eng, "_keepwarm_loop", noop)
    monkeypatch.setattr(eng, "_refresh_universe_loop", noop)

    async def go():
        await eng.start(feeds=True)
        await asyncio.gather(*eng._tasks, return_exceptions=True)
        await eng.http.aclose()
    asyncio.run(go())
    st = eng.ledger.feed_status("x")
    assert st["connected"] is False and st["info"] == {"enabled": False}


def test_transient_setting_read_error_keeps_previous_value(eng, monkeypatch):
    import sqlite3
    eng.ledger.set_setting("shadow_enabled", "0")
    eng._shadow_checked_ts = 0.0
    assert eng.shadow_enabled is False

    def locked(db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(engine_mod, "shadow_enabled_from", locked)
    eng._shadow_checked_ts = 0.0
    assert eng.shadow_enabled is False         # previous cached value, not the env default
