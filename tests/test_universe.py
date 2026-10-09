from fastlane.universe import tokens


def test_tokens():
    t = tokens("Fed’s Powell says CPI 2025 up-or-down A&B")
    assert t == ["fed's", "powell", "cpi", "up-or-down", "a&b"]


def test_rare_name_first(tiny_universe):
    out = tiny_universe.shortlist("Avient reports results")
    assert out and out[0]["id"] == "AVNT-1" and out[0]["match_score"] > 0


def test_ladder_collapses_to_two_most_liquid(tiny_universe):
    out = tiny_universe.shortlist("CPI inflation")
    ids = [m["id"] for m in out]
    assert len(ids) == 2 and set(ids) == {"CPI-B", "CPI-C"}


def test_min_score(tiny_universe):
    assert tiny_universe.shortlist("bitcoin") == []
    assert tiny_universe.shortlist("bitcoin", min_score=0)


def test_k_respected(tiny_universe):
    out = tiny_universe.shortlist("bitcoin senate rainfall avient inflation", k=2, min_score=0)
    assert len(out) == 2
    assert len(tiny_universe.shortlist("bitcoin senate rainfall avient inflation", k=5, min_score=0)) > 2


def test_unrelated_query_empty(tiny_universe):
    assert tiny_universe.shortlist("zzzz qqqq") == []
