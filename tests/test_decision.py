import pytest

from fastlane import books, decision
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


GOOD = dict(decisive_yes=.6, toward_yes=.3, no_signal=.1)      # strength .9, decisive .6
STRONGER = dict(decisive_yes=.7, toward_yes=.25, no_signal=.05) # strength .95, decisive .7


def test_max_entry_price_matches_books():
    assert decision.MAX_ENTRY_PRICE == books.MAX_ENTRY_PRICE == .95


def test_no_room_only_qualifier_is_pass_priced_in():
    r = decide({"m0": ans(**GOOD)}, {"m0": {"yes_ask": .97, "yes_bid": .96}})
    assert r["action"] == "PASS" and r["reason"] == "priced_in" and r["key"] == "m0" and r["side"] == "yes"
    assert r["strength"] == pytest.approx(.9) and r["room"] == pytest.approx(.03)


def test_no_room_buy_no_side_is_pass_priced_in():
    # NO entry = 1 - yes_bid = .98 -> room .02
    r = decide({"m0": ans(decisive_no=.6, toward_no=.3, no_signal=.1)}, {"m0": {"yes_ask": .04, "yes_bid": .02}})
    assert (r["action"], r["reason"], r["side"]) == ("PASS", "priced_in", "no")
    assert r["room"] == pytest.approx(.02)


def test_no_room_strongest_loses_to_roomy_qualifier():
    r = decide({"m0": ans(**STRONGER), "m1": ans(**GOOD)},
               {"m0": {"yes_ask": .97, "yes_bid": .96}, "m1": {"yes_ask": .4, "yes_bid": .38}})
    assert r["key"] == "m1" and r["action"] == "BUY_YES" and r["reason"] == "signal_yes"


def test_priced_in_picks_strongest_no_room_qualifier():
    r = decide({"m0": ans(**GOOD), "m1": ans(**STRONGER)},
               {"m0": {"yes_ask": .96, "yes_bid": .95}, "m1": {"yes_ask": .98, "yes_bid": .97}})
    assert (r["action"], r["reason"], r["key"]) == ("PASS", "priced_in", "m1")


def test_priced_in_not_overridden_by_roomy_non_qualifier():
    weak = ans(decisive_yes=.4, toward_yes=.1, no_signal=.5)
    r = decide({"m0": ans(**GOOD), "m1": weak},
               {"m0": {"yes_ask": .97, "yes_bid": .96}, "m1": {"yes_ask": .4, "yes_bid": .38}})
    assert (r["action"], r["reason"], r["key"]) == ("PASS", "priced_in", "m0")


def test_room_boundary_at_max_entry_price():
    at = decide({"m0": ans(**GOOD)}, {"m0": {"yes_ask": .95, "yes_bid": .94}})     # room == 1 - .95: no room
    assert (at["action"], at["reason"]) == ("PASS", "priced_in")
    below = decide({"m0": ans(**GOOD)}, {"m0": {"yes_ask": .94, "yes_bid": .93}})  # room .06: trades
    assert (below["action"], below["reason"]) == ("BUY_YES", "signal_yes")


def test_unknown_price_still_qualifies():
    for keyed in (None, {"m0": {}}, {"m0": {"yes_ask": None, "yes_bid": None}}):
        r = decide({"m0": ans(**GOOD)}, keyed)
        assert (r["action"], r["reason"]) == ("BUY_YES", "signal_yes") and r["room"] == .5


def test_weak_signal_with_no_room_stays_weak_signal():
    r = decide({"m0": ans(decisive_yes=.4, toward_yes=.1, no_signal=.5)}, {"m0": {"yes_ask": .97, "yes_bid": .96}})
    assert (r["action"], r["reason"]) == ("PASS", "weak_signal")
