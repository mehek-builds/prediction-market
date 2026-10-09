import asyncio
import time

import pytest

from fastlane import kalshi_tape
from fastlane.kalshi_tape import MOVE_COOLDOWN_S, KalshiTape


class StubLedger:
    def __init__(self):
        self.ticks = []

    def tick(self, *a):
        self.ticks.append(a)


INFO = {"category": "Politics", "volume_24h": 1000, "question": "Q"}


def make(info=INFO):
    calls = []
    tape = KalshiTape(StubLedger(), market_info=lambda t: info, on_move=lambda *a: calls.append(a),
                      deny_re=kalshi_tape.move_deny_re())
    return tape, calls


def test_move_fires():
    tape, calls = make()
    t = 1000.0
    tape.hist["EV-A"].append((t - 30, .40, .42))
    tape._check_move("EV-A", t, .50, .52)
    assert len(calls) == 1 and tape.moves == 1
    ticker, info, before, now = calls[0]
    assert ticker == "EV-A" and before == (t - 30, .40, .42) and now == (t, .50, .52)


def test_downward_move_fires():
    tape, calls = make()
    tape.hist["EV-A"].append((970, .50, .52))
    tape._check_move("EV-A", 1000, .40, .42)
    assert len(calls) == 1


def test_no_move_cases():
    t = 1000.0
    for bid, ask in [(.43, .45), (.50, .42 + .001), (.50, .60), (0, .52)]:
        tape, calls = make()
        tape.hist["EV-A"].append((t - 30, .40, .42))
        tape._check_move("EV-A", t, bid, ask)
        assert calls == [], (bid, ask)


def test_only_one_side_moved():
    tape, calls = make()
    tape.hist["EV-A"].append((970, .40, .42))
    tape._check_move("EV-A", 1000, .50, .43)
    assert calls == []


def test_filters():
    for info in ({"category": "Crypto", "volume_24h": 1000}, {"category": "Politics", "volume_24h": 10}, None):
        tape, calls = make(info)
        tape.hist["EV-A"].append((970, .40, .42))
        tape._check_move("EV-A", 1000, .50, .52)
        assert calls == []


def test_cooldown_per_event_key():
    tape, calls = make()
    tape.hist["EV-A"].append((970, .40, .42))
    tape.hist["EV-B"].append((970, .40, .42))
    tape._check_move("EV-A", 1000, .50, .52)
    tape._check_move("EV-B", 1000 + 5, .50, .52)
    assert len(calls) == 1
    later = 1000 + MOVE_COOLDOWN_S + 40
    tape.hist["EV-B"].append((later - 30, .40, .42))
    tape._check_move("EV-B", later, .50, .52)
    assert len(calls) == 2


def test_quote_queries_and_track():
    tape, _ = make()
    for q in [(100, .4, .42), (110, .41, .43), (120, .42, .44)]:
        tape.hist["T"].append(q)
    assert tape.quote("T") == (120, .42, .44) and tape.quote("nope") is None
    assert tape.quote_at("T", 115) == (110, .41, .43)
    assert tape.quote_at("T", 99) is None  # history does not reach back
    tape.track("T", 110)
    assert tape.ledger.ticks == [("T", 110, .41, .43), ("T", 120, .42, .44)]
    assert tape.tracked["T"] > time.time()


def test_run_without_sign_headers_returns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(kalshi_tape.websockets, "connect", boom)
    tape = KalshiTape(StubLedger(), sign_headers=None)
    assert asyncio.run(asyncio.wait_for(tape.run(), 2)) is None


GAS = {"category": "Economics", "volume_24h": 1000, "question": "US gas prices tomorrow: Above 4.36"}


def test_deny_gas_ladders_by_title_and_ticker():
    for info, ticker in ((GAS, "KXAAAGAS-25OCT10-T4.36"),
                         ({**GAS, "question": "Minnesota gas prices tomorrow: Above 3.10"}, "KXMNGAS-25OCT10-T3.10"),
                         ({**GAS, "question": "Average AAA: Above 4.36"}, "KXAAAGAS-25OCT10-T4.36"),
                         ({**GAS, "question": "Bitcoin price on Oct 15 at 5pm EDT: Above 120,000"}, "KXBTC-25OCT15-T120000"),
                         ({**GAS, "question": "Gold price this week: Above 4,000"}, "KXGOLDW-25OCT17-T4000")):
        tape, calls = make(info)
        tape.hist[ticker].append((970, .40, .42)); tape._check_move(ticker, 1000, .50, .52)
        assert calls == [] and tape.moves_denied == 1, info["question"]


def test_denied_move_does_not_set_cooldown():
    tape, calls = make(GAS)
    tape.hist["KXAAAGAS-25OCT10-T4.36"].append((970, .40, .42))
    tape._check_move("KXAAAGAS-25OCT10-T4.36", 1000, .50, .52)
    assert calls == [] and tape.moves == 0 and tape._cooldown == {}


def test_deny_pattern_lets_news_markets_through():
    for q in ("Fed decision in October: Cut 25 bps", "Will the price of eggs be discussed at the debate?",
              "Government shutdown ends by Oct 20?"):
        tape, calls = make({"category": "Economics", "volume_24h": 1000, "question": q})
        tape.hist["KXSHUT-25OCT20"].append((970, .40, .42)); tape._check_move("KXSHUT-25OCT20", 1000, .50, .52)
        assert len(calls) == 1, q


def test_deny_env_override_and_validation(monkeypatch):
    monkeypatch.setenv("MOVE_DENY_RE", "^NEVER$")
    tape, calls = make(GAS); tape.deny_re = kalshi_tape.move_deny_re()
    tape.hist["KXAAAGAS-25OCT10-T4.36"].append((970, .40, .42)); tape._check_move("KXAAAGAS-25OCT10-T4.36", 1000, .50, .52)
    assert len(calls) == 1                                      # env pattern replaces the default
    monkeypatch.setenv("MOVE_DENY_RE", "(unclosed")
    with pytest.raises(ValueError):
        kalshi_tape.move_deny_re()
    monkeypatch.setenv("MOVE_DENY_RE", "")
    assert kalshi_tape.move_deny_re().pattern == kalshi_tape.MOVE_DENY_DEFAULT


# ---------- v0.5.0: on_tick hook for resting paper orders ----------
def test_on_tick_fires_only_for_watched_tickers_synchronously_with_the_quote():
    calls = []
    tape = KalshiTape(StubLedger(), on_tick=lambda *a: calls.append(a))
    tape.watch.add("EV-A")
    tape._record("EV-A", 1000.0, .40, .42)
    tape._record("EV-B", 1000.0, .40, .42)               # not watched
    assert calls == [("EV-A", 1000.0, .40, .42)]
    assert tape.hist["EV-B"] and tape.msgs == 2          # still recorded, just no callback


def test_on_tick_skips_volume_only_updates_and_unwatched_after_discard():
    calls = []
    tape = KalshiTape(StubLedger(), on_tick=lambda *a: calls.append(a))
    tape.watch.add("EV-A")
    tape._record("EV-A", 1000.0, .40, .42)
    tape._record("EV-A", 1001.0, .40, .42)               # same quote: not a tick
    tape._record("EV-A", 1002.0, .41, .43)
    assert [c[1] for c in calls] == [1000.0, 1002.0]
    tape.watch.discard("EV-A")
    tape._record("EV-A", 1003.0, .50, .52)
    assert len(calls) == 2


def test_on_tick_runs_after_history_is_extended():
    seen = []
    tape = KalshiTape(StubLedger())
    tape.on_tick = lambda t, ts, b, a: seen.append(tape.quote(t))
    tape.watch.add("EV-A")
    tape._record("EV-A", 1000.0, .40, .42)
    assert seen == [(1000.0, .40, .42)]


def test_a_failing_on_tick_never_breaks_the_tape(capsys):
    def boom(*a):
        raise RuntimeError("order bug")
    tape = KalshiTape(StubLedger(), on_tick=boom)
    tape.watch.add("EV-A")
    tape._record("EV-A", 1000.0, .40, .42)
    tape._record("EV-A", 1001.0, .41, .43)
    assert tape.msgs == 2 and "on_tick error" in capsys.readouterr().out


def test_no_hook_and_tracked_ticks_still_persist():
    tape = KalshiTape(StubLedger())
    tape.watch.add("EV-A")
    tape.tracked["EV-A"] = time.time() + 100
    tape._record("EV-A", time.time(), .40, .42)
    assert len(tape.ledger.ticks) == 1
