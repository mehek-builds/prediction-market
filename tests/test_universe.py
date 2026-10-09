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


import pytest

from fastlane.universe import is_sports_market


@pytest.mark.parametrize("m", [
    {"question": "Spread: New York Liberty (-3.5)"},
    {"question": "Lakers vs. Celtics"},
    {"question": "Counter-Strike: FURIA vs MOUZ (BO3) - ESL Pro League Playoffs"},
    {"question": "Knicks vs Nets: O/U 221.5"},
    {"question": "Yankees Moneyline"},
    {"question": "Total Points: Over 48.5"},
    {"question": "Chiefs 1H Spread (-2.5)"},
    {"question": "Warriors Q4 Moneyline"},
    {"question": "Will Team A win?", "sportsMarketType": "moneyline"},
    {"question": "Anything", "gameId": "1711477"},
])
def test_sports_markets_detected(m):
    assert is_sports_market(m)


@pytest.mark.parametrize("m", [
    {"question": "Will New York City ban gas stoves by 2027?"},
    {"question": "Will the Fed cut rates in December?"},
    {"question": "New York mayoral election: Will Mamdani win?"},
    {"question": "Will Amex open a Centurion Lounge in Boston?"},
    {"question": "Will Trump vs Biden rematch polling lead exceed 5 points?"},
    {"question": "Will the Dallas Fed president resign?", "sportsMarketType": None, "gameId": None},
])
def test_non_sports_markets_kept(m):
    assert not is_sports_market(m)


@pytest.mark.parametrize("q", [
    "Spread: Chiefs (-6.5)", "Over/Under: Lakers vs Celtics O/U 224.5", "Celtics vs. Heat", "Real Madrid vs Barcelona",
    "LoL: T1 vs Gen.G (BO5) - Worlds Finals", "Total Goals: Over 2.5", "Lakers 2H Moneyline", "Warriors Q3 total",
    "Dodgers vs. Padres: Total Runs Over 8.5", "Moneyline: Bills", "Moneyline Q4: Lakers", "Lakers 2H Total",
    "Celtics Q4 Moneyline", "Chiefs 1H Spread", "Lakers Total Q3",
])
def test_real_looking_sports_lines_detected(q):
    assert is_sports_market({"question": q})


@pytest.mark.parametrize("q", [
    "Will the Lakers vs Celtics series go to 7 games?",         # starts with Will: never a game line
    "Fed decision in December?", "Bitcoin above $120,000 on October 31?", "Will Trump win the 2028 election?",
    "Will the Senate pass the budget bill before November?", "US government shutdown by Oct 31?",
    "Will Elon Musk tweet more than 50 times this week?", "Will the 1st quarter GDP print exceed 3%?",
    "Highest temperature in NYC on Oct 12?", "Will Nvidia be the largest company by market cap?",
    "Will total tariffs revenue exceed $100B?",
    "Will US GDP growth in Q3 2026 be above 2%?", "Will Nvidia beat Q4 earnings estimates?",
    "Will Tesla deliver more than 400k vehicles in Q2?", "Will the S&P 500 close up in 1H 2026?",
    "Total US national debt above $40T by end of 2026?", "Harris vs Trump popular vote margin?",
    "Will Apple report Q1 revenue above $100B?", "Q2 GDP growth above 3%?", "Will Amazon Q3 total revenue top $150B?",
    "Will the Fed cut rates in 2H 2026?", "Will total US jobs added exceed 200k?",
    "Biden vs Trump: who wins the popular vote?",
    "Tesla Q2 deliveries total above 400k?", "Total US tariff revenue in Q3 above $100B?",
    "Nobel Peace Prize winner 2026 announced in Q4?", "Bitcoin vs Gold: which performs better in 2026",
    "Trump vs. Biden debate", "Apple Q1 total revenue above $100B", "Total US population above 340M in 2026?",
])
def test_real_looking_non_sports_kept(q):
    assert not is_sports_market({"question": q})


def test_missing_or_empty_question_is_not_sports():
    assert not is_sports_market({}) and not is_sports_market({"question": None}) and not is_sports_market({"question": ""})


class _Resp:
    def __init__(self, body):
        self._b = body
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._b


def _gamma(i, q, **kw):
    return {"id": str(i), "question": q, "clobTokenIds": '["y%d","n%d"]' % (i, i), "bestAsk": .5, "bestBid": .48,
            "volume24hr": 10, "enableOrderBook": True, **kw}


def test_poly_skips_sports_and_keeps_the_rest():
    import asyncio
    from fastlane.universe import Universe
    batch = [_gamma(1, "Will the Fed cut rates in December?"),
             _gamma(2, "Lakers vs. Celtics", sportsMarketType="moneyline"),
             _gamma(3, "Anything at all", gameId="1711477"),
             _gamma(4, "Spread: Chiefs (-6.5)"),
             _gamma(5, "Bitcoin above 120k on Oct 31?")]

    class C:
        async def get(self, url, params=None, **kw):
            return _Resp(batch)

    out = asyncio.run(Universe()._poly(C()))
    assert [m["id"] for m in out][:2] == ["1", "5"] and not {"2", "3", "4"} & {m["id"] for m in out}
    assert out[0]["venue"] == "polymarket" and out[0]["yes_token"] == "y1"


def test_cache_load_prunes_cached_polymarket_sports_only(tmp_path, monkeypatch):
    import asyncio
    import json
    from fastlane import universe as uni
    cache = tmp_path / "universe.json"
    mk = lambda venue, id, q: dict(venue=venue, id=id, question=q, category="", yes_ask=.5, yes_bid=.48, volume_24h=1)  # noqa: E731
    cache.write_text(json.dumps([mk("polymarket", "1", "Will the Fed cut rates in December?"),
                                 mk("polymarket", "2", "Lakers vs. Celtics"),
                                 mk("kalshi", "K1", "Celtics vs. Heat winner")]))     # kalshi rows are left alone
    monkeypatch.setattr(uni, "CACHE", cache)
    u = uni.Universe()
    assert asyncio.run(u.load(None)) == "cache"
    assert sorted(m["id"] for m in u.markets) == ["1", "K1"]
    assert len(json.loads(cache.read_text())) == 3            # the cache file itself is not rewritten
