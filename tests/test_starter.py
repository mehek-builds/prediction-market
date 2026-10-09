"""The starter book: a small paper book (strength >= 0.90, no decisive requirement, $20 a trade) that is isolated from
the real book and can never send a real order. Offline."""
import asyncio
import time

import httpx
import pytest

from fastlane import engine as engine_mod
from fastlane import live
from fastlane.books import Book

from test_engine_handle import FakeJev, _answers_for, _event, _make_engine, _run
from test_live import FILLED, Recorder
from test_live import _engine as _live_engine

STRONG_LEAN = {"toward_yes": .87, "decisive_yes": .05, "no_signal": .08}     # strength .92, decisive .05
MID_LEAN = {"toward_yes": .83, "decisive_yes": .05, "no_signal": .12}         # strength .88


def _eng(monkeypatch, tmp_path, tiny_universe, probs=STRONG_LEAN, **kw):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, **kw)
    e.starter_on = True                     # _make_engine switches the starter off for the older tests
    e.jev = FakeJev(_answers_for(probs=probs))
    return e


def _books(e):
    return sorted(r[0] for r in e.ledger.db.execute("SELECT book FROM trades"))


def _row(e):
    return e.ledger.db.execute(
        "SELECT starter_action, starter_reason, starter_market_id FROM decisions WHERE event_id='ev1'").fetchone()


def test_lean_only_signal_trades_shadow_and_starter_but_not_real(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    rec = _run(e, _event())
    assert rec["reason"] == "lean_not_decisive"
    assert (rec["starter_action"], rec["starter_reason"], rec["starter_market_id"]) == ("BUY_YES", "signal_yes", "AVNT-1")
    assert _books(e) == ["shadow", "starter"]
    assert e.ledger.db.execute("SELECT shadow FROM trades WHERE book='starter'").fetchone() == (0,)   # spec: shadow=0
    keys = {r[0] for r in e.ledger.db.execute("SELECT event_id FROM marks WHERE horizon_s = 0")}
    assert keys == {"ev1", "shadow:ev1", "starter:ev1"}
    assert e.ledger.traded_markets() == set() and e.ledger.spent_today() == 0
    assert e.trades == 0 and e.shadow_trades == 1 and e.starter_trades == 1
    assert _row(e) == ("BUY_YES", "signal_yes", "AVNT-1")


def test_starter_never_counts_in_real_pnl_even_when_deeply_underwater(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    _run(e, _event())
    e.ledger.mark("starter:ev1", 5, .01, .005, .007)
    e.ledger.mark("shadow:ev1", 5, .01, .005, .007)
    assert e._today_pnl() == 0.0
    assert e.ledger.spent_today() == 0 and e.ledger.traded_markets("live") == set()
    assert e.ledger.traded_markets("starter") == {"AVNT-1"}


def test_below_threshold_starter_passes_weak_signal_shadow_trades(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe, probs=MID_LEAN)
    rec = _run(e, _event())
    assert (rec["starter_action"], rec["starter_reason"]) == ("PASS", "weak_signal")
    assert _books(e) == ["shadow"]


def test_threshold_from_env_lets_the_weaker_signal_trade(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("STARTER_SIGNAL_THRESHOLD", "0.80")
    e = _eng(monkeypatch, tmp_path, tiny_universe, probs=MID_LEAN)
    assert e.starter_signal == .80
    rec = _run(e, _event())
    assert rec["starter_reason"] == "signal_yes" and _books(e) == ["shadow", "starter"]


def test_size_is_twenty_dollars_by_default_and_env_overridable(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    _run(e, _event())
    cost = e.ledger.db.execute("SELECT cost FROM trades WHERE book='starter'").fetchone()[0]
    assert 0 < cost <= 20.0
    shadow_cost = e.ledger.db.execute("SELECT cost FROM trades WHERE book='shadow'").fetchone()[0]
    assert shadow_cost > 20.0          # shadow uses the full per-trade size
    monkeypatch.setenv("STARTER_SIZE_USD", "5")
    (tmp_path / "b").mkdir()
    e2 = _eng(monkeypatch, tmp_path / "b", tiny_universe)
    _run(e2, _event())
    assert 0 < e2.ledger.db.execute("SELECT cost FROM trades WHERE book='starter'").fetchone()[0] <= 5.0


def test_disabled_leaves_no_starter_columns_and_no_trade(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("STARTER_ENABLED", "false")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.jev = FakeJev(_answers_for(probs=STRONG_LEAN))
    assert e.starter_on is False
    rec = _run(e, _event())
    assert "starter_action" not in rec and _books(e) == ["shadow"]
    assert _row(e) == (None, None, None)


def test_real_buy_intent_is_real_signalled_for_starter(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe, probs={"decisive_yes": .8, "toward_yes": .15, "no_signal": .05})
    rec = _run(e, _event())
    assert rec["action"] == "BUY_YES"
    assert (rec["starter_action"], rec["starter_reason"]) == (None, "real_signalled")
    assert _books(e) == ["live"]


def test_stale_news_blocks_the_starter(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    rec = _run(e, _event(age_s=engine_mod.MAX_NEWS_AGE_S + 60))
    assert rec["starter_reason"] == "stale_news" and _books(e) == []


def test_cost_block_is_too_expensive(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe,
             book=lambda mid: Book("kalshi", mid, [(.55, 1000)], [(.50, 1000)]))
    rec = _run(e, _event())
    assert (rec["starter_action"], rec["starter_reason"]) == ("PASS", "too_expensive") and _books(e) == []


def test_sports_market_is_refused(monkeypatch, tmp_path, tiny_universe):
    tiny_universe.markets[3]["category"] = "Sports"
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    rec = _run(e, _event())
    assert rec["starter_reason"] == "sports_market"
    assert _books(e) == ["shadow"]      # only the starter carries the sports guard


def test_second_event_on_the_same_market_is_already_in_market(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    _run(e, _event())
    e.http = httpx.AsyncClient()
    rec2 = _run(e, _event(id="ev2"))
    assert rec2["starter_reason"] == "already_in_market"
    assert _books(e) == ["shadow", "starter"]


def test_working_starter_order_also_blocks_the_next_event(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    e.styles["starter"] = "post"
    rec = _run(e, _event())
    assert rec["starter_reason"] == "post_working" and _books(e) == ["shadow"]
    assert [o["book"] for o in e.ledger.orders_working()] == ["starter"]
    e.http = httpx.AsyncClient()
    rec2 = _run(e, _event(id="ev2"))
    assert rec2["starter_reason"] == "already_in_market" and len(e.ledger.orders_working()) == 1


def test_synthetic_events_skip_the_starter(monkeypatch, tmp_path, tiny_universe):
    e = _eng(monkeypatch, tmp_path, tiny_universe)
    rec = _run(e, _event(synthetic=True))
    assert "starter_action" not in rec and _books(e) == []


def test_starter_never_reaches_the_real_order_path(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec_ = Recorder(FILLED, FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    e.starter_on = True
    e.styles["starter"] = "take"
    e.jev = FakeJev(_answers_for(probs=STRONG_LEAN))
    live.arm(e.live.session)

    async def go():
        try:
            await e.handle(_event())
            await e.drain_shadow()
            await e.drain_starter()
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    asyncio.run(go())
    assert _books(e) == ["shadow", "starter"]
    assert rec_.requests == [] and e.ledger.db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_shadow_and_starter_share_one_prefetch_and_neither_cancels_the_others_book(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("STARTER_SIGNAL_THRESHOLD", "0.60")
    a = {"venue": "kalshi", "id": "AVNT-2", "question": "Avient quarterly earnings beat estimates", "category": "Companies",
         "yes_ask": .31, "yes_bid": .29, "volume_24h": 200}
    tiny_universe._set(tiny_universe.markets + [a])
    fetched = []

    def book(mid):
        fetched.append(mid)
        return (Book("kalshi", mid, [(.31, 1000)], [(.71, 1000)]) if mid == "AVNT-2"
                else Book("kalshi", mid, [(.55, 1000)], [(.47, 1000)]))

    def answers(questions):
        out = {}
        for k, q in questions.items():
            if "Avient" not in q["instructions"]:
                out[k] = {"probabilities": {"no_signal": 1.0}}
            elif "estimates" in q["instructions"]:
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .35, "no_signal": .35}}   # .65, roomy
            else:
                out[k] = {"probabilities": {"decisive_yes": .3, "toward_yes": .4, "no_signal": .3}}      # .70
        return out
    e = _make_engine(monkeypatch, tmp_path, tiny_universe, book=book)
    e.starter_on = True
    e.jev = FakeJev(answers)
    rec = _run(e, _event())
    assert rec["market_id"] == "AVNT-1" and rec["shadow_market_id"] == "AVNT-2" and rec["starter_market_id"] == "AVNT-2"
    assert sorted(fetched) == ["AVNT-1", "AVNT-2"]               # one fetch per market, shared
    assert e.ledger.db.execute("SELECT market_id, book FROM trades ORDER BY book").fetchall() == \
        [("AVNT-2", "shadow"), ("AVNT-2", "starter")]


def test_pool_last_release_cancels_pending_tasks():
    async def go():
        t = asyncio.ensure_future(asyncio.sleep(5))
        pool = engine_mod.PrefetchPool({"x": t})
        pool.acquire(); pool.acquire()
        pool.release()
        await asyncio.sleep(0)
        assert not t.cancelled()
        pool.release()
        await asyncio.sleep(0)
        assert t.cancelled()
    asyncio.run(go())
