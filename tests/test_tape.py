import asyncio
import time

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
    tape = KalshiTape(StubLedger(), market_info=lambda t: info, on_move=lambda *a: calls.append(a))
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
