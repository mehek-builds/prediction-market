"""v0.6.0: move detector denies count and tally ladders (Oct 9 replay), no-bid P&L, daily-loss halt, clean shutdown,
days_label, dashboard wording, hygiene. Offline."""
import asyncio
import os
import re
import signal
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastlane import api, live
from fastlane import engine as engine_mod
from fastlane.books import Book
from fastlane.kalshi_tape import KalshiTape, move_deny_re
from fastlane.ledger import Ledger
from fastlane.x_feed import days_label

from test_engine_handle import FakeJev, _answers_for, _make_engine, _run
from test_tape import StubLedger

ROOT = Path(__file__).resolve().parent.parent

WEEK = {"venue": "kalshi", "id": "KXTRUTHSOCIALW-26OCT10-B80", "category": "Politics", "yes_ask": .30, "yes_bid": .28,
        "volume_24h": 900, "question": "Trump Truth Social posts this week? (10/4-10/10): 80-99"}
SHUTDOWN = {"venue": "kalshi", "id": "KXGOVSHUT-26OCT20", "category": "Politics", "yes_ask": .40, "yes_bid": .38,
            "volume_24h": 900, "question": "Government shutdown ends by Oct 20?"}


# ---------------------------------------------------------------- tape
def tape_for(question, category="Politics"):
    calls = []
    tape = KalshiTape(StubLedger(), market_info=lambda t: {"category": category, "volume_24h": 1000, "question": question},
                      on_move=lambda *a: calls.append(a), deny_re=move_deny_re())
    return tape, calls


def fire(question, ticker, category="Politics"):
    tape, calls = tape_for(question, category)
    tape.hist[ticker].append((970, .40, .42))
    tape._check_move(ticker, 1000, .50, .52)
    return tape, calls


DENY = [
    ("Trump Truth Social posts on Oct 9, 2026?: 5-9", "KXTRUTHSOCIAL-26OCT09-B5"),
    ("Trump Truth Social posts this week? (10/4-10/10): 80-99", "KXTRUTHSOCIALW-26OCT10-B80"),
    ("How many times will Trump say tariff during the speech?: 3-5", "KXSAYS-26OCT12-B3"),
    ("Elon Musk tweets this week?: 300-349", "KXTWEETS-26OCT10-B300"),
    ("How many times will Powell mention inflation?: 10+", "KXMENTION-26OCT29-T10"),
    ("How many times will X say Y", "KXOTHER-26OCT09-B1"),                                   # title-only hit, neutral ticker
    ("Something bland: 5-9", "KXTRUTHSOCIAL-26OCT09-B5"),                                    # ticker-only hit, bland title
]
ALLOW = [
    ("Fed decision in October: Cut 25 bps", "KXFEDDECISION-26OCT-C25"),
    ("Government shutdown ends by Oct 20?", "KXGOVSHUT-26OCT20"),
    ("Will Trump sign the bill by Nov 1?", "KXTRUMPSIGN-26NOV01"),
    ("Trump approval above 45% on Oct 31?", "KXAPPROVE-26OCT31-T45"),
    ("Will Trump be banned from Truth Social?", "KXTRUMPBAN-26"),
    ("Will Truth Social parent DJT be delisted?", "KXDJTDELIST-26"),
    ("Will the SEC sue Elon Musk over his tweets?", "KXMUSKSEC-26"),
    ("Will Musk's tweets move Tesla?", "KXMUSKTSLA-26"),
    ("Will a debt ceiling deal happen?", "KXSAYDEBTCEILING-26"),                              # ticker merely starts KXSAY
]


@pytest.mark.parametrize("question,ticker", DENY)
def test_count_and_tally_ladders_are_denied_without_cooldown(question, ticker):
    tape, calls = fire(question, ticker)
    assert calls == [] and tape.moves_denied == 1 and tape.moves == 0 and tape._cooldown == {}


def test_deny_regex_matches_ticker_series_boundaries_only():
    rx = move_deny_re()
    for tk in ("KXTRUTHSOCIAL-26OCT09-B5", "KXTRUTHSOCIALW-26OCT10-B80", "KXSAYS-26OCT12-B3", "KXTWEET-26OCT10-B1"):
        assert rx.search(tk), tk
    for tk in ("KXSAYDEBTCEILING", "KXSAYDEBTCEILING-26", "KXTRUTHSOCIALBAN-26"):
        assert not rx.search(tk), tk


@pytest.mark.parametrize("question,ticker", ALLOW)
def test_real_political_and_economic_markets_still_fire(question, ticker):
    tape, calls = fire(question, ticker, "Economics" if "Fed" in question else "Politics")
    assert len(calls) == 1 and tape.moves_denied == 0


def test_a_custom_deny_regex_replaces_the_default():
    r = move_deny_re({"MOVE_DENY_RE": "^ZZZ"})
    assert r.search("ZZZ-1") and not r.search("KXTRUTHSOCIAL-26OCT09-B5")
    assert move_deny_re({}).search("KXTRUTHSOCIAL-26OCT09-B5")
    with pytest.raises(ValueError):
        move_deny_re({"MOVE_DENY_RE": "(unclosed"})


# ---------------------------------------------------------------- engine: move events drop ladders as candidates
def _universe(tiny_universe, extra):
    tiny_universe._set(tiny_universe.markets + extra)
    return tiny_universe


def _move_event(headline, ticker="KXOTHER-1"):
    return {"id": f"move-{ticker}-1", "source": "kalshi_move", "headline": headline, "summary": "", "url": "",
            "published_ts": time.time(), "seen_ts": time.time(), "exclude_series": ticker.split("-", 1)[0]}


def _news_event(headline):
    return {"id": "news-1", "source": "cnbc", "headline": headline, "summary": "", "url": "", "published_ts": time.time() - 5,
            "seen_ts": time.time()}


def test_a_count_ladder_is_never_a_candidate_of_a_move_event(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, _universe(tiny_universe, [WEEK]))
    rec = _run(e, _move_event("Trump Truth Social posts this week climbs on a big repricing"))
    assert (rec["action"], rec["reason"], rec["n_candidates"]) == ("PASS", "no_candidates", 0)
    assert e.jev.calls == 0 and e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_the_same_ladder_stays_a_candidate_of_a_news_event(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, _universe(tiny_universe, [WEEK]))
    e.jev = FakeJev(_answers_for(target_key_text="Truth Social"))
    _run(e, _news_event("Trump Truth Social posts this week hit a record"))
    assert e.jev.calls == 1
    assert any("Truth Social" in q["instructions"] for q in e.jev.last_questions.values())


def test_a_real_non_count_market_is_still_a_candidate_of_a_move_event(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, _universe(tiny_universe, [WEEK, SHUTDOWN]))
    e.jev = FakeJev(_answers_for(target_key_text="shutdown"))
    _run(e, _move_event("Government shutdown talks repriced from 50% to 60%"))
    asked = " ".join(q["instructions"] for q in e.jev.last_questions.values())
    assert e.jev.calls == 1 and "Government shutdown ends by Oct 20" in asked and "Truth Social" not in asked


def test_the_october_9_posts_move_is_denied_end_to_end(monkeypatch, tmp_path, tiny_universe):
    title, ticker = "Trump Truth Social posts on Oct 9, 2026?: 5-9", "KXTRUTHSOCIAL-26OCT09-B5"
    # 1. the tape: 63% -> 88% in 15 s on the day ladder is denied, no event, no cooldown
    tape, calls = tape_for(title)
    tape.hist[ticker].append((970, .62, .64))
    tape._check_move(ticker, 1000, .87, .89)
    assert calls == [] and tape.moves_denied == 1 and tape._cooldown == {}
    # 2. an event built by _on_move anyway (tape bypassed) is handled with the week ladder in the universe: no trade
    e = _make_engine(monkeypatch, tmp_path, _universe(tiny_universe, [WEEK]))
    e._on_move(ticker, {"question": title}, (970, .62, .64), (1000, .87, .89))
    ev = e.queue.get_nowait()
    assert ev["source"] == "kalshi_move" and ev["exclude_series"] == "KXTRUTHSOCIAL"
    rec = _run(e, ev)
    assert (rec["action"], rec["reason"]) == ("PASS", "no_candidates")
    assert e.jev.calls == 0 and e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    # the move event no longer triggers: the engine owns the same compiled pattern as the tape
    assert e.deny_re.search(ticker) and e.deny_re.search(title) and e.tape.deny_re is e.deny_re


# ---------------------------------------------------------------- no-bid P&L
@pytest.fixture
def nobid(tmp_ledger, tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    api._books.clear()
    api._markets.clear()
    holder = {"book": None}

    async def fake(market):
        return holder["book"]
    monkeypatch.setattr(api, "_book", fake)
    L = tmp_ledger

    def add(eid, market, side, cost=4.0, fee=.05):
        L.event({"id": eid, "source": "cnbc", "headline": "h", "url": "http://x", "published_ts": now - 100, "seen_ts": now - 99})
        L.decision(eid, total_ms=400, action="BUY_YES", reason="signal_yes", venue="kalshi", market_id=market,
                   market_question="Q?", decided_ts=now - 98)
        L.trade(event_id=eid, opened_ts=now - 90, venue="kalshi", market_id=market, market_question="Q?", side=side,
                contracts=10, avg_price=.40, cost=cost, fee=fee, best_ask=.40, synthetic=0, book="live")
    return TestClient(api.app), add, holder


def _trade(c):
    return c.get("/trades").json()


def test_yes_position_with_no_yes_bid_is_valued_at_zero_and_counted(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    holder["book"] = Book("kalshi", "MK1", [(.62, 5)], [])                       # no NO asks: nobody bids for YES
    d = _trade(c)
    t = d["trades"][0]
    assert t["now_price"] == 0 and t["no_bid"] is True and t["value"] == 0
    assert t["pnl"] == pytest.approx(-(4.0 + .05)) and t["pnl_pct"] == pytest.approx(-100)
    assert t["path"][-1]["price"] == 0
    assert d["summary"]["pnl"] == pytest.approx(-4.05) and d["summary"]["books"]["live"]["pnl"] == pytest.approx(-4.05)
    assert d["summary"]["books"]["live"]["losers"] == 1


def test_no_position_with_no_yes_asks_is_valued_at_zero(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "no")
    holder["book"] = Book("kalshi", "MK1", [], [(.50, 5)])                       # no YES asks: nobody bids for NO
    t = _trade(c)["trades"][0]
    assert t["now_price"] == 0 and t["no_bid"] is True and t["pnl"] == pytest.approx(-4.05)


def test_zero_bid_counts_as_no_bid(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    holder["book"] = Book("kalshi", "MK1", [(.62, 5)], [(1.0, 5)])               # bid = 1 - 1.0 = 0
    t = _trade(c)["trades"][0]
    assert t["no_bid"] is True and t["now_price"] == 0


def test_a_book_with_a_bid_is_not_flagged(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    holder["book"] = Book("kalshi", "MK1", [(.62, 5)], [(.50, 5)])
    t = _trade(c)["trades"][0]
    assert t["no_bid"] is False and t["now_price"] == pytest.approx(.50)


def test_empty_book_is_unknown_not_a_total_loss(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    add("e2", "MK2", "no")
    holder["book"] = Book("kalshi", "MK1", [], [])                               # closed or settled market
    d = _trade(c)
    for t in d["trades"]:
        assert t["now_price"] is None and t["no_bid"] is False and t["value"] is None and t["pnl"] is None
        assert all(p["price"] is not None for p in t["path"])
    assert d["summary"]["pnl"] == 0 and d["summary"]["books"]["live"]["losers"] == 0


def test_book_fetch_failure_stays_unknown_not_zero(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    holder["book"] = None
    d = _trade(c)
    t = d["trades"][0]
    assert t["now_price"] is None and t["value"] is None and t["pnl"] is None and t["no_bid"] is False
    assert d["summary"]["pnl"] == 0


def test_a_no_bid_loss_and_a_priced_win_add_up(nobid):
    c, add, holder = nobid
    add("e1", "MK1", "yes")
    add("e2", "MK2", "yes")
    books = {"MK1": Book("kalshi", "MK1", [(.62, 5)], []), "MK2": Book("kalshi", "MK2", [(.62, 5)], [(.50, 5)])}

    async def fake(market):
        return books[market["id"]]
    api._books.clear()
    import fastlane.api as a
    a._book = fake
    d = _trade(c)
    assert d["summary"]["pnl"] == pytest.approx((10 * .50 - 4.05) + (0 - 4.05))


# ---------------------------------------------------------------- dashboard
HTML = (ROOT / "fastlane/static/index.html").read_text()


def _fn(name):
    m = re.search(rf"function {name}\(.*?\n\}}\n", HTML, re.S)
    assert m, name
    return m.group(0)


def test_dashboard_card_and_row_render_no_bid_as_text():
    for name in ("trRow", "card"):
        body = _fn(name)
        assert "t.no_bid" in body and '"no bid"' in body, name
        assert "innerHTML" not in body
    assert HTML.count('"no bid"') >= 2


def test_dashboard_gained_no_buttons_or_inputs():
    now = re.findall(r"<(button|input)\b", HTML)
    import subprocess
    head = subprocess.run(["git", "show", "HEAD:fastlane/static/index.html"], cwd=ROOT, capture_output=True, text=True)
    if head.returncode == 0:
        assert len(now) == len(re.findall(r"<(button|input)\b", head.stdout))


# ---------------------------------------------------------------- daily loss halt
def _live_engine(monkeypatch, tmp_path, tiny_universe):
    return _make_engine(monkeypatch, tmp_path, tiny_universe)


def _put_trade(e, eid, side="yes", cost=40.0, fee=.5, contracts=100, mark=None, synthetic=0):
    now = time.time()
    e.ledger.event({"id": eid, "source": "cnbc", "headline": "h", "url": "", "published_ts": now - 50, "seen_ts": now - 49})
    e.ledger.trade(event_id=eid, opened_ts=now - 40, venue="kalshi", market_id=f"M-{eid}", market_question="Q", side=side,
                   contracts=contracts, avg_price=cost / contracts, cost=cost, fee=fee, best_ask=.4, synthetic=synthetic, book="live")
    if mark is not None:
        e.ledger.mark(eid, 5, *mark)               # (yes_ask, yes_bid, mid)


def test_a_mark_without_a_bid_counts_as_a_total_loss(monkeypatch, tmp_path, tiny_universe):
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    _put_trade(e, "a", mark=(.6, None, None))
    assert e._today_pnl() == pytest.approx(-(40.0 + .5))


def test_a_mark_with_both_prices_null_is_unknown_and_never_halts(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("PAPER_DAILY_LOSS_HALT_PCT", "0.05")
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    for i in range(3):                                     # settled winners: the market closed, the mark is empty
        _put_trade(e, f"w{i}", side="yes" if i % 2 else "no", cost=200.0, fee=.5, contracts=500, mark=(None, None, None))
    assert e._today_pnl() == 0.0
    assert e._risk_block("FRESH", False) is None


def test_a_no_trade_with_no_yes_ask_mark_counts_as_total_loss(monkeypatch, tmp_path, tiny_universe):
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    _put_trade(e, "a", side="no", mark=(None, .3, None))
    assert e._today_pnl() == pytest.approx(-40.5)


def test_a_trade_without_any_mark_is_excluded_as_unknown(monkeypatch, tmp_path, tiny_universe):
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    _put_trade(e, "a")
    assert e._today_pnl() == 0.0


def test_a_priced_mark_still_uses_the_bid(monkeypatch, tmp_path, tiny_universe):
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    _put_trade(e, "a", mark=(.6, .55, .575))
    assert e._today_pnl() == pytest.approx(100 * .55 - 40.5)


def test_losses_from_no_bid_trades_trigger_daily_loss_halt(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("PAPER_DAILY_LOSS_HALT_PCT", "0.05")
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    assert e._risk_block("FRESH", False) is None
    for i in range(3):                                     # 3 x 200.5 lost vs a halt of 5% of the bankroll (500)
        _put_trade(e, f"t{i}", cost=200.0, fee=.5, contracts=500, mark=(.5, None, None))
    assert e._today_pnl() < -e.halt_loss
    assert e._risk_block("FRESH", False) == "daily_loss_halt"


def test_synthetic_trades_never_count_toward_the_halt(monkeypatch, tmp_path, tiny_universe):
    e = _live_engine(monkeypatch, tmp_path, tiny_universe)
    _put_trade(e, "s", synthetic=1, mark=(.5, None, None))
    assert e._today_pnl() == 0.0


# ---------------------------------------------------------------- clean shutdown
def test_sigterm_and_sigint_set_the_stop_event_within_a_second():
    async def go(sig):
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        installed = engine_mod.install_stop_signals(loop, stop)
        assert installed == [signal.SIGTERM, signal.SIGINT]
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(stop.wait(), 1.0)
        for s in installed:
            loop.remove_signal_handler(s)
        return stop.is_set()
    assert asyncio.run(go(signal.SIGTERM)) is True
    assert asyncio.run(go(signal.SIGINT)) is True


def test_a_second_signal_after_the_first_falls_back_to_the_default_handler():
    async def go():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        engine_mod.install_stop_signals(loop, stop)
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(stop.wait(), 1.0)
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGINT)         # the second Ctrl-C is no longer swallowed
            await asyncio.sleep(0.5)
    asyncio.run(go())


def test_a_stopped_engine_reads_as_dead_and_a_fresh_trader_may_start(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.live.start()
    assert live.engine_alive(live.read_engine()) is True
    asyncio.run(e.stop())
    assert live.engine_alive(live.read_engine()) is False
    assert live.read_engine()["heartbeat_ts"] == 0
    fresh = live.LiveTrader(Ledger(tmp_path / "other.db"), None, None)
    fresh.start()                                           # does not raise "another fastlane engine is already running"


def test_a_live_heartbeat_blocks_a_second_engine_control(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.live.start()
    other = live.LiveTrader(Ledger(tmp_path / "other.db"), None, None)
    with pytest.raises(Exception, match="already running"):
        other.start()
    asyncio.run(e.stop())


def test_run_help_text_and_signal_wiring_in_source():
    src = (ROOT / "fastlane/run.py").read_text()
    assert "until Ctrl-C or SIGTERM" in src and "install_stop_signals" in src and "stopping (signal)" in src
    assert "await eng.stop()" in src and "finally:" in src


# ---------------------------------------------------------------- days_label and start line
@pytest.mark.parametrize("days,want", [
    (frozenset(range(7)), "every day"),
    (frozenset({0, 1, 2, 3, 4}), "Mon-Fri"),
    (frozenset({0, 2, 4}), "Mon, Wed, Fri"),
    (frozenset({0, 1, 2, 5, 6}), "Mon-Wed, Sat-Sun"),
    (frozenset({2}), "Wed"),
    (frozenset({5, 6}), "Sat-Sun"),
])
def test_days_label(days, want):
    assert days_label(days) == want


def test_engine_start_line_uses_the_label_not_a_count():
    src = (ROOT / "fastlane/engine.py").read_text()
    assert "weekdays" not in src.split("x feed:")[1].split("\n")[1] and "days_label(s.days)" in src


# ---------------------------------------------------------------- hygiene
def test_version_changelog_head_and_rollback_line():
    import fastlane
    assert fastlane.__version__ == "0.6.0"
    heads = re.findall(r"^## (\d+\.\d+\.\d+) - \d{4}-\d{2}-\d{2}$", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert heads[0] == "0.6.0"
    assert "POLY_FOMC" in (ROOT / "ROLLBACK.md").read_text() or "0.6.0" in (ROOT / "ROLLBACK.md").read_text()


def test_env_example_poly_vars_carry_the_code_defaults_and_no_secret():
    from fastlane import releases
    text = (ROOT / ".env.example").read_text()
    d = releases.settings({})
    assert re.search(r"^POLY_FOMC_ENABLED=true$", text, re.M) and d.poly_fomc is True
    assert re.search(r"^POLY_FOMC_MAX_NO=1$", text, re.M) and d.poly_max_no == 1


def test_poly_settings_defaults_and_errors():
    from fastlane import releases
    s = releases.settings({})
    assert s.poly_fomc is True and s.poly_max_no == 1
    assert releases.settings({"POLY_FOMC_ENABLED": "false"}).poly_fomc is False
    assert releases.settings({"POLY_FOMC_MAX_NO": "0"}).poly_max_no == 0
    assert releases.settings({"POLY_FOMC_MAX_NO": "-3"}).poly_max_no == 0
    with pytest.raises(ValueError, match="POLY_FOMC_MAX_NO"):
        releases.settings({"POLY_FOMC_MAX_NO": "lots"})


def test_fixture_directory_is_complete():
    fx = ROOT / "tests" / "fixtures" / "polymarket_fomc"
    for name in ("meta.json", "search-2026-10.json", "event-2026-10.json", "statement-2026-09-16.html"):
        assert (fx / name).exists(), name
    assert len(list((fx / "markets").glob("*.json"))) == 5 and len(list((fx / "books").glob("*.json"))) == 5


# ---------------------------------------------------------------- ledger and report: release_books
def test_release_books_roundtrip_is_ordered_and_additive(tmp_ledger):
    L = tmp_ledger
    base = dict(release_id="fomc-2026-10", yes_bid=.4, yes_ask=.42, bid_qty=10, ask_qty=20)
    L.release_book_put(market_id="B", label="+5s", ts=105.0, **base)
    L.release_book_put(market_id="A", label="decision", ts=100.0, **base)
    L.release_book_put(market_id="A", label="decision", ts=100.0, **{**base, "yes_bid": .45})      # same key replaces
    rows = L.release_books("fomc-2026-10")
    assert [(r["market_id"], r["label"]) for r in rows] == [("A", "decision"), ("B", "+5s")]
    assert rows[0]["yes_bid"] == .45 and L.release_books("other") == []


def test_report_prints_one_line_per_market_with_the_four_labels(tmp_ledger, capsys):
    from fastlane import report
    for lb, ts, bid, ask in [("decision", 100.0, .62, .64), ("+5s", 105.0, .90, .92), ("+30s", 130.0, .95, .96), ("+60s", 160.0, None, .97)]:
        tmp_ledger.release_book_put(release_id="fomc-2026-10", market_id="2589811", label=lb, ts=ts, yes_bid=bid, yes_ask=ask,
                                    bid_qty=1, ask_qty=1)
    report.release_books_lines(tmp_ledger.db, 0)
    out = capsys.readouterr().out
    assert "books around fomc-2026-10" in out
    assert "decision 0.62/0.64  +5s 0.90/0.92  +30s 0.95/0.96  +60s -/0.97" in out
