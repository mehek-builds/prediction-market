import pytest

from fastlane.decision import LEVELS, build_request, decide


def ans(**p):
    return {"probabilities": p}


def test_qualifies_buy_yes():
    r = decide({"m0": ans(decisive_yes=.6, toward_yes=.3, no_signal=.1)}, {"m0": {"yes_ask": .4, "yes_bid": .38}})
    assert r["action"] == "BUY_YES" and r["reason"] == "signal_yes" and r["key"] == "m0" and r["side"] == "yes"
    assert r["strength"] == pytest.approx(0.9)
    assert r["p_decisive"] == pytest.approx(0.6)
    assert r["room"] == pytest.approx(0.6)


def test_buy_no_uses_bid_for_room():
    r = decide({"m0": ans(decisive_no=.6, toward_no=.3, no_signal=.1)}, {"m0": {"yes_ask": .4, "yes_bid": .38}})
    assert r["action"] == "BUY_NO" and r["reason"] == "signal_no" and r["side"] == "no"
    assert r["room"] == pytest.approx(.38)


def test_weak_signal():
    r = decide({"m0": ans(decisive_yes=.4, toward_yes=.1, no_signal=.5)}, {"m0": {"yes_ask": .4, "yes_bid": .38}})
    assert r["action"] == "PASS" and r["reason"] == "weak_signal" and r["key"] == "m0"


def test_lean_not_decisive():
    r = decide({"m0": ans(toward_yes=.9, decisive_yes=.1)}, {"m0": {"yes_ask": .4, "yes_bid": .38}})
    assert r["action"] == "PASS" and r["reason"] == "lean_not_decisive"


def test_picks_most_room():
    good = ans(decisive_yes=.6, toward_yes=.3, no_signal=.1)
    r = decide({"m0": good, "m1": good}, {"m0": {"yes_ask": .8, "yes_bid": .78}, "m1": {"yes_ask": .4, "yes_bid": .38}})
    assert r["key"] == "m1"


def test_irrelevant():
    r = decide({"m0": ans(no_signal=1.0)}, {"m0": {"yes_ask": .4, "yes_bid": .38}})
    assert r["action"] == "PASS" and r["reason"] == "irrelevant" and r["key"] is None


def test_no_answers():
    r = decide({})
    assert r["action"] == "PASS" and r["reason"] == "no_answers"


def test_missing_quote_room_is_half():
    r = decide({"m0": ans(decisive_yes=.6, toward_yes=.3, no_signal=.1)})
    assert r["room"] == .5


def test_build_request():
    cands = [{"question": "Will X happen?"}, {"question": "Will Y happen?"}]
    state, questions, keyed = build_request({"headline": "H", "summary": "S", "source": "src"}, cands)
    assert state["headline"] == "H"
    assert list(questions) == ["m0", "m1"] == list(keyed)
    for k, q in questions.items():
        assert q["type"] == "choice" and q["criteria"] == LEVELS
        assert keyed[k]["question"] in q["instructions"]
