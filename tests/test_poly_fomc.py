"""Polymarket FOMC brackets: template matching, discovery, bracket mapping, ranking, paper-only trades, rehearsal.
Offline: fixtures from tests/fixtures/polymarket_fomc and httpx.MockTransport."""
import asyncio
import copy
import json
import re
from pathlib import Path

import httpx
import pytest

from fastlane import live, poly_fomc, releases
from fastlane.books import Book
from fastlane.ledger import Ledger
from fastlane.releases import CalendarEntry, ReleaseScheduler, SERIES, resolves_yes, series_for

from test_releases import FOMC, MKT, _release_ev, _trade_release
from test_live import FILLED, Recorder
from test_live import _engine as _live_engine

FX = Path(__file__).parent / "fixtures" / "polymarket_fomc"
NOV = CalendarEntry("fomc", "2026-12-09", "14:00", "2026-11", prior_range=(3.75, 4.0))
EXPECTED = {"2589810": "cut_50", "2589811": "cut_25", "2589812": "hold", "2589813": "hike_25", "2589814": "hike_50"}


def _fx(name):
    f = FX / name
    if not f.exists():
        pytest.skip(f"fixture missing: {name}")
    return f


def event():
    body = json.loads(_fx("event-2026-10.json").read_text())
    return copy.deepcopy(body[0] if isinstance(body, list) else body)


def markets():
    return {m["id"]: m for m in event()["markets"]}


def search():
    return json.loads(_fx("search-2026-10.json").read_text())


# ---------------------------------------------------------------- question templates
@pytest.mark.parametrize("mid,bracket", sorted(EXPECTED.items()))
def test_each_captured_question_parses_to_its_bracket(mid, bracket):
    assert poly_fomc.parse_question(markets()[mid]["question"]) == (bracket, 2026, 10)


def test_reworded_other_month_other_year_and_trailing_text_are_refused():
    assert poly_fomc.parse_question("Will the Fed decrease interest rates by 25 bps after the November 2026 meeting?") == \
        ("cut_25", 2026, 11)                       # parses, but the meeting differs (see other_meeting below)
    assert poly_fomc.parse_question("Will the Fed cut rates by 25 bps after the October 2026 meeting?") is None
    assert poly_fomc.parse_question("Will the Fed decrease interest rates by 25 bps after the October 2026 meeting? (Yes)") is None
    assert poly_fomc.parse_question("Will the Fed decrease interest rates by 75 bps after the October 2026 meeting?") is None
    assert poly_fomc.parse_question("") is None and poly_fomc.parse_question(None) is None


def test_description_ok_needs_both_the_calendar_page_and_the_upper_bound():
    desc = markets()["2589811"]["description"]
    assert poly_fomc.description_ok(desc)
    assert not poly_fomc.description_ok(re.sub("upper bound", "range", desc, flags=re.I))
    assert not poly_fomc.description_ok(desc.replace("federalreserve.gov/monetarypolicy/fomccalendars.htm", "example.com"))
    assert not poly_fomc.description_ok("")


def test_every_captured_market_is_accepted_with_bracket_tokens_and_end_date():
    for mid, m in markets().items():
        pm, why = poly_fomc.parse_poly_market(m, FOMC)
        assert why == "" and pm is not None
        toks = json.loads(m["clobTokenIds"])
        assert (pm.ticker, pm.bracket, pm.venue, pm.series, pm.strike_type) == (mid, EXPECTED[mid], "polymarket", "POLYFED", "bracket")
        assert (pm.yes_token, pm.no_token, pm.close_time) == (toks[0], toks[1], m["endDate"])


def _mut(**kw):
    m = markets()["2589811"]
    m.update(kw)
    return m


@pytest.mark.parametrize("kw,reason", [
    ({"acceptingOrders": False}, "not_accepting"),
    ({"closed": True}, "not_accepting"),
    ({"active": False}, "not_accepting"),
    ({"outcomes": json.dumps(["Up", "Down"])}, "outcomes_mismatch"),
    ({"clobTokenIds": json.dumps(["only-one"])}, "tokens_missing"),
    ({"clobTokenIds": "not json"}, "tokens_missing"),
    ({"description": "Resolves on the upper bound but names no page."}, "description_mismatch"),
    ({"question": "Will the Fed cut rates by 25 bps after the October 2026 meeting?"}, "question_mismatch"),
    ({"question": "Will the Fed decrease interest rates by 25 bps after the October 2025 meeting?"}, "other_meeting"),
    ({"endDate": ""}, "no_end_date"),
])
def test_unacceptable_markets_are_refused_with_a_reason(kw, reason):
    pm, why = poly_fomc.parse_poly_market(_mut(**kw), FOMC)
    assert pm is None and why == reason


def test_the_november_entry_refuses_every_october_market():
    assert {poly_fomc.parse_poly_market(m, NOV)[1] for m in markets().values()} == {"other_meeting"}


def test_foreign_description_market_is_refused():
    m = _mut(description="Resolves per some other source. See example.com.")
    assert poly_fomc.parse_poly_market(m, FOMC)[1] == "description_mismatch"


# ---------------------------------------------------------------- discovery
def _transport(search_body=None, event_bodies=None, log=None):
    def handler(req):
        if log is not None:
            log.append(req)
        if req.url.path == "/public-search":
            return httpx.Response(200, json=search_body if search_body is not None else search())
        if req.url.path == "/events":
            slug = req.url.params["slug"]
            body = (event_bodies or {}).get(slug)
            return httpx.Response(200, json=body if body is not None else [event()])
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def _discover(transport, entry=FOMC, trace=None):
    n = []

    async with httpx.AsyncClient(transport=transport) as http:
        async def get(url, **kw):
            n.append(url)
            return await http.get(url, **kw)
        out = await poly_fomc.discover(http, get, entry, trace=trace)
    return out, n


def test_discover_accepts_five_with_exactly_two_gamma_gets():
    log = []
    (acc, rej), urls = asyncio.run(_discover(_transport(log=log)))
    assert len(acc) == 5 and rej == [] and {pm.bracket for pm in acc} == set(poly_fomc.BRACKETS)
    assert len(urls) == 2 and len(log) == 2 and {r.url.host for r in log} == {"gamma-api.polymarket.com"}
    assert {r.method for r in log} == {"GET"}


def test_discover_with_no_matching_title_gets_once_and_returns_nothing():
    body = {"events": [{"slug": "fed-decision-in-december", "title": "Fed Decision in December?"},
                       {"slug": "other", "title": "Something else"}]}
    (acc, rej), urls = asyncio.run(_discover(_transport(search_body=body)))
    assert (acc, rej) == ([], []) and len(urls) == 1


def test_discover_two_matching_events_is_ambiguous_and_accepts_nothing():
    s = search()
    ev2 = event()
    ev2["slug"] = "fed-decision-in-october-copy"
    s["events"].append({"slug": ev2["slug"], "title": "Fed Decision in October?"})
    (acc, rej), _ = asyncio.run(_discover(_transport(search_body=s, event_bodies={ev2["slug"]: [ev2]})))
    assert acc == [] and rej == [("", "", "ambiguous_event")]


def test_discover_reports_rejected_markets_and_fills_the_trace():
    ev = event()
    ev["markets"][0]["acceptingOrders"] = False
    trace = {}
    (acc, rej), _ = asyncio.run(_discover(_transport(event_bodies={ev["slug"]: [ev]}), trace=trace))
    assert len(acc) == 4 and [(r[0], r[2]) for r in rej] == [("2589810", "not_accepting")]
    assert set(trace["markets"]) == set(EXPECTED) and trace["slugs"] == [ev["slug"]]


def test_discover_ignores_an_event_for_another_month():
    (acc, rej), _ = asyncio.run(_discover(_transport(), entry=NOV))
    assert acc == [] and rej == []


def test_discover_http_error_raises():
    t = httpx.MockTransport(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_discover(t))


# ---------------------------------------------------------------- still_open
def _still(body):
    async def go():
        t = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
        async with httpx.AsyncClient(transport=t) as http:
            return await poly_fomc.still_open(http, http.get, "2589811")
    return asyncio.run(go())


def test_still_open_true_for_the_fixture_market_and_false_when_flags_flip():
    m = json.loads(_fx("markets/2589811.json").read_text())
    assert _still(m) == (True, "")
    assert _still({**m, "acceptingOrders": False})[0] is False
    assert _still({**m, "closed": True})[0] is False
    assert _still({**m, "active": False})[0] is False


# ---------------------------------------------------------------- outcome mapping
def _pms():
    return [poly_fomc.parse_poly_market(m, FOMC)[0] for m in markets().values()]


@pytest.mark.parametrize("bps,want", [(-50, "cut_50"), (-25, "cut_25"), (0, "hold"), (25, "hike_25"), (50, "hike_50")])
def test_each_bps_change_resolves_exactly_one_bracket(bps, want):
    assert [pm.bracket for pm in _pms() if resolves_yes(pm, float(bps))] == [want]


@pytest.mark.parametrize("bps", [12, 75, -75, -100, 100, 13])
def test_a_change_outside_the_map_resolves_no_bracket(bps):
    assert not any(resolves_yes(pm, float(bps)) for pm in _pms())


def test_polyfed_is_a_registered_polymarket_series_for_fomc():
    assert series_for("fomc") == ["KXFED", "KXFEDDECISION", "POLYFED"]
    assert SERIES["POLYFED"].venue == "polymarket" and SERIES["KXFED"].venue == "kalshi"
    assert releases.on_boundary(_pms()[0], -25.0) is False


# ---------------------------------------------------------------- rank_brackets
def sched(env=None, tmp=None):
    return ReleaseScheduler(Ledger(tmp) if tmp else None, None, None, None, cfg=releases.settings(env or {}), verbose=False)


def bk(pm, ya=None, na=None, qty=100):
    return Book("polymarket", pm.ticker, [(ya, qty)] if ya is not None else [], [(na, qty)] if na is not None else [])


def books_for(**spec):
    """spec: bracket=(yes_ask, no_ask)."""
    pms = _pms()
    return pms, {pm.ticker: bk(pm, *spec[pm.bracket]) for pm in pms}


FIVE = dict(cut_50=(.03, .96), cut_25=(.70, .31), hold=(.20, .82), hike_25=(.08, .93), hike_50=(.03, .97))


def test_rank_yes_on_the_resolving_bracket_then_the_lowest_no_ask():
    pms, books = books_for(**FIVE)
    picks = sched().rank_brackets(-25.0, books, pms)
    assert [(pm.bracket, side) for pm, side, _b, _p in picks] == [("cut_25", "yes"), ("hold", "no")]


def test_rank_max_no_zero_gives_yes_only():
    pms, books = books_for(**FIVE)
    picks = sched({"POLY_FOMC_MAX_NO": "0"}).rank_brackets(-25.0, books, pms)
    assert [(pm.bracket, side) for pm, side, *_ in picks] == [("cut_25", "yes")]


def test_rank_expensive_yes_ask_drops_the_yes_pick_but_not_the_no_picks():
    pms, books = books_for(**{**FIVE, "cut_25": (.96, .05)})
    picks = sched().rank_brackets(-25.0, books, pms)
    assert [(pm.bracket, side) for pm, side, *_ in picks] == [("hold", "no")]


def test_rank_one_sided_book_on_the_resolving_bracket_is_skipped():
    pms, books = books_for(**FIVE)
    cut25 = next(pm for pm in pms if pm.bracket == "cut_25")
    books[cut25.ticker] = bk(cut25, ya=.70, na=None)              # no NO ask means no YES bid
    picks = sched().rank_brackets(-25.0, books, pms)
    assert all(pm.bracket != "cut_25" for pm, *_ in picks)


def test_rank_one_sided_dead_bracket_is_skipped_and_the_next_room_is_used():
    pms, books = books_for(**FIVE)
    hold = next(pm for pm in pms if pm.bracket == "hold")
    books[hold.ticker] = bk(hold, ya=.20, na=.82)
    books[hold.ticker] = Book("polymarket", hold.ticker, [], [(.82, 100)])      # no YES ask: no NO bid
    picks = sched().rank_brackets(-25.0, books, pms)
    assert [pm.bracket for pm, side, *_ in picks][1:] == ["hike_25"]


def test_rank_max_no_four_gives_at_most_four_no_picks():
    pms, books = books_for(**{**FIVE, "cut_50": (.12, .90), "hike_25": (.12, .91), "hike_50": (.12, .92)})
    picks = sched({"POLY_FOMC_MAX_NO": "4"}).rank_brackets(-25.0, books, pms)
    sides = [side for _pm, side, *_ in picks]
    assert sides[0] == "yes" and sides.count("no") == 4 and len(picks) == 5
    nos = [px for _pm, side, _b, px in picks if side == "no"]
    assert nos == sorted(nos)


def test_rank_ties_keep_market_order():
    pms, books = books_for(**{**FIVE, "cut_50": (.03, .90), "hike_50": (.10, .90), "hold": (.20, .90), "hike_25": (.08, .90)})
    picks = sched({"POLY_FOMC_MAX_NO": "4"}).rank_brackets(-25.0, books, pms)
    assert [pm.bracket for pm, side, *_ in picks if side == "no"] == ["cut_50", "hold", "hike_25", "hike_50"]


def test_rank_returns_nothing_when_no_market_matches_the_resolving_bracket():
    pms, books = books_for(**FIVE)
    assert sched().rank_brackets(12.0, books, pms) == []                          # outside the map
    rest = [pm for pm in pms if pm.bracket != "cut_25"]
    assert sched().rank_brackets(-25.0, books, rest) == []                        # live bracket missing: no dead guess


def test_rank_skips_markets_without_a_fresh_book():
    pms, books = books_for(**FIVE)
    del books[next(pm for pm in pms if pm.bracket == "cut_25").ticker]
    picks = sched().rank_brackets(-25.0, books, pms)
    assert all(side == "no" for _pm, side, *_ in picks)


# ---------------------------------------------------------------- paper only by construction
def test_gate_restated_polymarket_is_paper_only():
    t = live.LiveTrader.__new__(live.LiveTrader)
    assert live.LiveTrader.gate(t, "polymarket", "2589811", False) == "paper_only_market"


def test_armed_engine_sends_zero_order_requests_on_a_polymarket_release_trade(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec)
    live.arm(e.live.session)
    market = {"venue": "polymarket", "id": "2589811", "question": markets()["2589811"]["question"],
              "category": "Economics", "yes_token": "y", "no_token": "n"}
    book = Book("polymarket", "2589811", [(.70, 1000)], [(.31, 1000)])
    ev = {**_release_ev(series="POLYFED"), "id": "release-POLYFED-2026-10"}

    async def go():
        try:
            return await e.trade_release(ev, market, "yes", book, -25.0, n_candidates=5, style="take")
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    out = asyncio.run(go())
    row = e.ledger.db.execute("SELECT venue, book, market_id, side FROM trades").fetchall()
    assert row == [("polymarket", "live", "2589811", "yes")]
    assert rec.requests == []
    assert isinstance(out, dict) and out.get("filled") is True and out.get("fill_ts")


def test_poly_fomc_module_has_no_order_path():
    src = Path(poly_fomc.__file__).read_text()
    assert ".post(" not in src and '"POST"' not in src and not re.search(r"/orders?\b", src)


# ---------------------------------------------------------------- rehearsal
def _run_rehearsal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = []
    rc = asyncio.run(releases.rehearse("fomc", FX, out=lines.append))
    return rc, "\n".join(map(str, lines))


def test_rehearsal_returns_zero_with_the_expected_trade_and_snapshots(tmp_path, monkeypatch):
    for n in ("meta.json", "statement-2026-07-29.html"):
        _fx(n)
    meta = json.loads((FX / "meta.json").read_text())
    rc, text = _run_rehearsal(tmp_path, monkeypatch)
    assert rc == 0, text
    assert "REHEARSAL" in text and meta["expected_bracket"] in text and "paper only" in text
    for label in ("decision", "+5s", "+30s", "+60s"):
        assert label in text
    assert "timing: statement seen" in text
    yes_id = next(i for i, b in EXPECTED.items() if b == meta["expected_bracket"])
    assert re.search(rf"{yes_id}\W+yes", text, re.I), text
    assert not (tmp_path / "fastlane" / "results").exists()


def test_rehearsal_uses_a_temp_ledger_and_creates_no_results_directory(tmp_path, monkeypatch):
    from fastlane import config
    import os
    before = {p: os.path.getmtime(p) for p in Path(config.ROOT).glob("fastlane/results/*")} if (Path(config.ROOT) / "fastlane/results").exists() else None
    rc, _ = _run_rehearsal(tmp_path, monkeypatch)
    after = {p: os.path.getmtime(p) for p in Path(config.ROOT).glob("fastlane/results/*")} if (Path(config.ROOT) / "fastlane/results").exists() else None
    assert rc == 0 and before == after
    assert list(tmp_path.iterdir()) == []


def test_rehearsal_missing_fixture_exits_two(tmp_path):
    lines = []
    assert asyncio.run(releases.rehearse("fomc", tmp_path, out=lines.append)) == 2
    assert any("fixture missing" in str(l) for l in lines)


def test_rehearsal_sends_requests_only_to_allowed_hosts(tmp_path, monkeypatch):
    seen = []
    orig = poly_fomc.fixture_transport

    def spy(*a, **k):
        tr = orig(*a, **k)
        seen.append(tr)
        return tr
    monkeypatch.setattr(poly_fomc, "fixture_transport", spy)
    rc, _ = _run_rehearsal(tmp_path, monkeypatch)
    assert rc == 0 and seen
    hosts = {r.url.host for r in seen[0].requests}
    assert hosts <= {"gamma-api.polymarket.com", "clob.polymarket.com", "www.federalreserve.gov"} | {releases.urlparse(__import__("fastlane.config", fromlist=["x"]).kalshi_api_url()).hostname}
    assert {r.method for r in seen[0].requests} == {"GET"}


def test_main_rehearse_fomc_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("fastlane.config.load_env", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert releases.main(["--rehearse", "fomc"]) == 0
    assert "REHEARSAL" in capsys.readouterr().out
    assert not (tmp_path / "fastlane" / "results").exists()


# ---------------------------------------------------------------- review fixes (H2, M2)
def test_description_naming_another_meeting_is_refused():
    desc = markets()["2589811"]["description"]
    assert "October 2026 meeting" in desc
    dec = desc.replace("October 2026 meeting", "December 2026 meeting")
    assert poly_fomc.parse_poly_market(_mut(description=dec), FOMC)[1] == "description_other_meeting"
    assert poly_fomc.parse_poly_market(_mut(description=desc), FOMC)[0] is not None


def test_a_december_event_is_refused_for_an_october_dated_entry_with_a_december_period():
    """The reviewer's case: date 2026-10-28, period 2026-12. The December brackets must never be armed."""
    entry = CalendarEntry("fomc", "2026-10-28", "14:00", "2026-12", prior_range=(3.75, 4.0))
    s = sched()
    said = []
    s.say = said.append

    async def boom(*a, **k):
        raise AssertionError("no request may be made for a period/date mismatch")
    s._get = boom
    out = asyncio.run(s._load_polymarket(entry, "POLYFED", 0.0))
    assert out == [] and entry.release_id not in s.poly_trace
    assert any("period_date_mismatch" in x for x in said)
    # and even if discovery were reached, October-worded markets do not pass for a December period
    dec_entry = CalendarEntry("fomc", "2026-12-09", "14:00", "2026-12", prior_range=(3.75, 4.0))
    assert {poly_fomc.parse_poly_market(m, dec_entry)[1] for m in markets().values()} == {"other_meeting"}


def test_two_markets_with_the_same_bracket_make_the_event_ambiguous():
    ev = event()
    dup = copy.deepcopy(ev["markets"][1])
    dup["id"] = "9999999"
    ev["markets"].append(dup)
    (acc, rej), _ = asyncio.run(_discover(_transport(event_bodies={ev["slug"]: [ev]})))
    assert acc == [] and rej == [("", "", "ambiguous_event")]
