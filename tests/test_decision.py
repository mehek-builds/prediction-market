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


LEAN = dict(toward_yes=.9, decisive_yes=.1)                     # strength 1.0, decisive .1: real lean_not_decisive
MID = dict(decisive_yes=.4, toward_yes=.4, no_signal=.2)        # strength .8: real weak_signal, shadow trades
LOW = dict(decisive_yes=.3, toward_yes=.35, no_signal=.35)      # strength .65: shadow trades (default .60), real passes
TOOLOW = dict(decisive_yes=.2, toward_yes=.3, no_signal=.5)     # strength .5: both pass
Q = {"m0": {"yes_ask": .4, "yes_bid": .38}}
PARITY_CASES = [  # (answers, keyed): every branch of the real rule
    ({"m0": ans(**GOOD)}, Q), ({"m0": ans(decisive_no=.6, toward_no=.3, no_signal=.1)}, Q),
    ({"m0": ans(**LEAN)}, Q), ({"m0": ans(**MID)}, Q), ({"m0": ans(**LOW)}, Q), ({"m0": ans(no_signal=1.0)}, Q), ({}, None),
    ({"m0": ans(**GOOD)}, {"m0": {"yes_ask": .97, "yes_bid": .96}}),
    ({"m0": ans(**STRONGER), "m1": ans(**GOOD)}, {"m0": {"yes_ask": .97, "yes_bid": .96}, "m1": {"yes_ask": .4, "yes_bid": .38}}),
    ({"m0": ans(**GOOD), "m1": ans(**GOOD)}, {"m0": {"yes_ask": .8, "yes_bid": .78}, "m1": {"yes_ask": .4, "yes_bid": .38}}),
    ({"m0": ans(**GOOD)}, {"m0": {"yes_ask": .95, "yes_bid": .94}}), ({"m0": ans(**GOOD)}, None),
]
# Pinned (action, reason) per case so parity is against known real-rule behavior, not only self-consistency.
PARITY_EXPECTED = [
    ("BUY_YES", "signal_yes"), ("BUY_NO", "signal_no"), ("PASS", "lean_not_decisive"), ("PASS", "weak_signal"),
    ("PASS", "weak_signal"), ("PASS", "irrelevant"), None,
    ("PASS", "priced_in"), ("BUY_YES", "signal_yes"), ("BUY_YES", "signal_yes"), ("PASS", "priced_in"),
    ("BUY_YES", "signal_yes"),
]


def test_real_rule_constants_unchanged():
    assert (decision.SIGNAL_THRESHOLD, decision.DECISIVE_MIN, decision.MARK_THRESHOLD) == (.85, .30, .30)
    assert (decision.SHADOW_SIGNAL_THRESHOLD, decision.SHADOW_DECISIVE_MIN, decision.SHADOW_ENABLED) == (.60, 0.0, True)
    assert decision.BUCKET_EDGES == (.60, .70, .85)


@pytest.mark.parametrize("answers,keyed", PARITY_CASES)
def test_real_rule_parity_with_explicit_thresholds(answers, keyed):
    assert decide(answers, keyed) == decide(answers, keyed, signal_threshold=.85, decisive_min=.30)


@pytest.mark.parametrize("case,expected", list(zip(PARITY_CASES, PARITY_EXPECTED)))
def test_real_rule_pinned_outcomes(case, expected):
    r = decide(*case)
    if expected is None:
        assert r["action"] == "PASS" and r["reason"] in ("no_answers", "no_candidates")
    else:
        assert (r["action"], r["reason"]) == expected


def test_shadow_trades_where_real_passes():
    r = decide({"m0": ans(**LEAN)}, Q)
    s = decide({"m0": ans(**LEAN)}, Q, signal_threshold=.60, decisive_min=0.0)
    assert (r["action"], r["reason"]) == ("PASS", "lean_not_decisive")
    assert (s["action"], s["reason"], s["key"]) == ("BUY_YES", "signal_yes", "m0")
    for probs in (MID, LOW):
        assert decide({"m0": ans(**probs)}, Q)["action"] == "PASS"
        assert decide({"m0": ans(**probs)}, Q, signal_threshold=.60, decisive_min=0.0)["action"] == "BUY_YES"
    assert decide({"m0": ans(**TOOLOW)}, Q, signal_threshold=.60, decisive_min=0.0)["reason"] == "weak_signal"


def test_shadow_keeps_room_rule_and_fallbacks():
    s = decide({"m0": ans(**LEAN)}, {"m0": {"yes_ask": .97, "yes_bid": .96}}, signal_threshold=.60, decisive_min=0.0)
    assert (s["action"], s["reason"]) == ("PASS", "priced_in")
    s = decide({"m0": ans(**LEAN), "m1": ans(**MID)},
               {"m0": {"yes_ask": .8, "yes_bid": .78}, "m1": {"yes_ask": .4, "yes_bid": .38}},
               signal_threshold=.60, decisive_min=0.0)
    assert s["key"] == "m1"  # most room wins, as in the real rule
    assert decide({"m0": ans(no_signal=1.0)}, Q, signal_threshold=.60, decisive_min=0.0)["reason"] == "irrelevant"


def test_settings_from_env(monkeypatch):
    assert decision.shadow_settings() == (True, .60, 0.0)
    assert decision.cost_settings() == (.03, .25, .03)
    monkeypatch.setenv("SHADOW_ENABLED", "false"); monkeypatch.setenv("SHADOW_SIGNAL_THRESHOLD", "0.8")
    monkeypatch.setenv("SHADOW_DECISIVE_MIN", "0.1"); monkeypatch.setenv("MAX_SPREAD_CENTS", "5")
    monkeypatch.setenv("COST_TO_ROOM_MAX", "0.5")
    assert decision.shadow_settings() == (False, .8, .1) and decision.cost_settings() == (.05, .5, .03)
    monkeypatch.setenv("MIN_ENTRY_PRICE", "0.05")
    assert decision.cost_settings()[2] == .05
    monkeypatch.setenv("SHADOW_SIGNAL_THRESHOLD", "")  # empty value falls back to the default
    assert decision.shadow_settings()[1] == .60


def test_strength_bucket():
    assert [decision.strength_bucket(s) for s in (.59, .60, .69, .70, .84, .85, 1.0)] == \
        ["<0.60", "0.60-0.70", "0.60-0.70", "0.70-0.85", "0.70-0.85", "0.85+", "0.85+"]
    assert decision.strength_bucket(None) is None


# ---------- v0.5.0: live quotes in selection ----------
def _lq(ask, bid):
    return {"yes_ask": ask, "yes_bid": bid}


@pytest.mark.parametrize("answers,keyed", PARITY_CASES)
def test_quotes_none_and_empty_are_parity(answers, keyed):
    base = decide(answers, keyed)
    assert decide(answers, keyed, quotes=None) == base
    assert decide(answers, keyed, quotes={}) == base


@pytest.mark.parametrize("case,expected", list(zip(PARITY_CASES, PARITY_EXPECTED)))
def test_live_quotes_equal_to_cache_keep_every_prior_branch(case, expected):
    answers, keyed = case
    quotes = {k: dict(v) for k, v in (keyed or {}).items()}
    r = decide(answers, keyed, quotes=quotes)
    if expected is None:
        assert r["action"] == "PASS"
    else:
        assert (r["action"], r["reason"]) == expected


def test_live_quote_overrides_cache_to_priced_in():
    r = decide({"m0": ans(**GOOD)}, {"m0": _lq(.40, .38)}, quotes={"m0": _lq(.97, .96)})
    assert (r["action"], r["reason"]) == ("PASS", "priced_in") and r["quote_source"] == "live"


def test_live_quote_overrides_cache_to_buy():
    r = decide({"m0": ans(**GOOD)}, {"m0": _lq(.97, .96)}, quotes={"m0": _lq(.40, .38)})
    assert r["action"] == "BUY_YES" and r["quote_source"] == "live" and r["entry"] == pytest.approx(.40)


def test_cache_quote_source_and_entry():
    r = decide({"m0": ans(**GOOD)}, {"m0": _lq(.40, .38)})
    assert r["quote_source"] == "cache" and r["entry"] == pytest.approx(.40)
    r = decide({"m0": ans(**GOOD)})
    assert r["quote_source"] == "none" and r["entry"] is None


def test_live_quotes_reorder_two_qualifiers():
    keyed = {"A": _lq(.30, .28), "B": _lq(.80, .78)}
    assert decide({"A": ans(**GOOD), "B": ans(**GOOD)}, keyed)["key"] == "A"
    r = decide({"A": ans(**GOOD), "B": ans(**GOOD)}, keyed, quotes={"A": _lq(.995, .99), "B": _lq(.60, .58)})
    assert r["key"] == "B" and r["action"] == "BUY_YES"


def test_missing_key_in_quotes_falls_back_to_cache():
    keyed = {"A": _lq(.30, .28), "B": _lq(.80, .78)}
    r = decide({"A": ans(**GOOD), "B": ans(**GOOD)}, keyed, quotes={"B": _lq(.10, .08)})
    assert r["key"] == "B" and r["entry"] == pytest.approx(.10)
    r = decide({"A": ans(**GOOD), "B": ans(**GOOD)}, keyed, quotes={"B": _lq(.99, .98)})
    assert r["key"] == "A" and r["quote_source"] == "cache"


def test_no_side_uses_live_yes_bid():
    a = {"m0": ans(decisive_no=.6, toward_no=.3, no_signal=.1)}
    r = decide(a, {"m0": _lq(.60, .58)}, quotes={"m0": _lq(.04, .02)})
    assert (r["action"], r["reason"], r["side"]) == ("PASS", "priced_in", "no")
    assert r["entry"] == pytest.approx(.98)
    r = decide(a, {"m0": _lq(.04, .02)}, quotes={"m0": _lq(.60, .58)})
    assert r["action"] == "BUY_NO" and r["entry"] == pytest.approx(.42)


def test_live_book_without_ask_is_room_zero_pass_no_fill():
    r = decide({"m0": ans(**GOOD)}, {"m0": _lq(.40, .38)}, quotes={"m0": _lq(None, .38)})
    assert (r["action"], r["reason"]) == ("PASS", "no_fill_within_limit")
    assert r["room"] == 0.0 and r["no_ask"] is True and r["entry"] is None


def test_live_no_bid_blocks_no_side_entry():
    a = {"m0": ans(decisive_no=.6, toward_no=.3, no_signal=.1)}
    for bid in (None, 0):
        r = decide(a, {"m0": _lq(.40, .38)}, quotes={"m0": _lq(.40, bid)})
        assert (r["action"], r["reason"]) == ("PASS", "no_fill_within_limit")


def test_no_ask_candidate_loses_to_roomy_qualifier():
    r = decide({"m0": ans(**STRONGER), "m1": ans(**GOOD)}, {},
               quotes={"m0": _lq(None, None), "m1": _lq(.50, .48)})
    assert r["key"] == "m1" and r["action"] == "BUY_YES"


def test_no_ask_vs_priced_in_picks_strongest_reason():
    r = decide({"m0": ans(**GOOD), "m1": ans(**STRONGER)}, {},
               quotes={"m0": _lq(.97, .96), "m1": _lq(None, None)})
    assert r["key"] == "m1" and r["reason"] == "no_fill_within_limit"
    r = decide({"m0": ans(**STRONGER), "m1": ans(**GOOD)}, {},
               quotes={"m0": _lq(.97, .96), "m1": _lq(None, None)})
    assert r["key"] == "m0" and r["reason"] == "priced_in"


def test_starter_settings_defaults_and_env(monkeypatch):
    assert decision.starter_settings() == (True, .90, 0.0, 20.0)
    monkeypatch.setenv("STARTER_ENABLED", "false"); monkeypatch.setenv("STARTER_SIGNAL_THRESHOLD", "0.8")
    monkeypatch.setenv("STARTER_DECISIVE_MIN", "0.1"); monkeypatch.setenv("STARTER_SIZE_USD", "35")
    assert decision.starter_settings() == (False, .8, .1, 35.0)
    monkeypatch.setenv("STARTER_SIZE_USD", "")
    assert decision.starter_settings()[3] == 20.0


def test_entry_styles_defaults_and_overrides(monkeypatch):
    assert decision.entry_styles() == {"live": "take", "shadow": "post", "starter": "post"}
    monkeypatch.setenv("ENTRY_STYLE", " POST ")
    assert decision.entry_styles() == {"live": "post", "shadow": "post", "starter": "post"}
    monkeypatch.setenv("ENTRY_STYLE", "take")
    monkeypatch.setenv("ENTRY_STYLE_SHADOW", "Post")
    assert decision.entry_styles() == {"live": "take", "shadow": "post", "starter": "take"}
    monkeypatch.setenv("ENTRY_STYLE_STARTER", "")
    assert decision.entry_styles()["starter"] == "take"   # empty means unset


@pytest.mark.parametrize("name", ["ENTRY_STYLE_LIVE", "ENTRY_STYLE_SHADOW", "ENTRY_STYLE_STARTER", "ENTRY_STYLE"])
def test_entry_styles_rejects_unknown(monkeypatch, name):
    monkeypatch.setenv(name, "limit")
    with pytest.raises(ValueError):
        decision.entry_styles()
