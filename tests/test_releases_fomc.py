"""v0.6.0 FOMC release path: prior_range verification, restart guard, polling cadence and cap, timing, release_books,
pre-order re-checks and entry style. Offline: httpx.MockTransport (Net), fake clock."""
import asyncio
import json

import httpx
import pytest

from fastlane import releases
from fastlane.books import Book

from test_releases import (FOMC, LINK, OTHER, POLY, PRIOR_LINK, PRIOR_PAGE, STATEMENT_CUT, Clock, Net, fed_feed, make,
                           ts, write_cal)

T = ts("2026-10-28", "14:00")
FED = "www.federalreserve.gov"
STMT_ITEM = ("Federal Reserve issues FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")


def fed_gets(net):
    return [(t, r) for t, r in net.reqs if r.url.host == FED]


def is_feed(r):
    return r.url.path.endswith("press_monetary.xml")


def is_url(r):
    return str(r.url).split("?")[0] == LINK


def wrap(net, fn):
    """Put `fn(req) -> Response | None` in front of the Net handler."""
    inner = net.handler
    net.all = []

    def handler(req):
        net.all.append(req)                    # includes answers the wrapper gave itself (Net only logs what it serves)
        out = fn(req)
        return out if out is not None else inner(req)
    net.handler = handler


def sched_for(ledger, net, clock, **kw):
    s = make(ledger, net, clock, **kw)
    s.http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))      # pick up a wrapped handler
    return s


# ---------------------------------------------------------------- verify_prior_range
def _arm(ledger, net, clock):
    s = sched_for(ledger, net, clock)
    return s, asyncio.run(s.arm(FOMC))


def test_prior_range_that_matches_the_statement_arms(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    s, armed = _arm(tmp_ledger, Net(clock), clock)
    assert armed is not None and "verified" in s.prior_check["fomc-2026-10"]
    assert tmp_ledger.release_get("fomc-2026-10")["status"] == "armed"


def test_prior_range_mismatch_refuses_to_arm(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.pages[PRIOR_LINK] = "<p>maintain the target range for the federal funds rate at 4 to 4-1/4 percent.</p>"
    s, armed = _arm(tmp_ledger, net, clock)
    row = tmp_ledger.release_get("fomc-2026-10")
    assert armed is None and row["status"] == "not_armed" and row["note"].startswith("prior_range_mismatch")


def test_prior_range_unverifiable_when_the_feed_errors(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    wrap(net, lambda r: httpx.Response(500) if is_feed(r) else None)
    s, armed = _arm(tmp_ledger, net, clock)
    row = tmp_ledger.release_get("fomc-2026-10")
    assert armed is None and row["note"].startswith("prior_range_unverified")


def test_prior_range_unverifiable_when_the_page_does_not_parse(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.pages[PRIOR_LINK] = "<p>Nothing useful.</p>"
    s, armed = _arm(tmp_ledger, net, clock)
    assert armed is None and tmp_ledger.release_get("fomc-2026-10")["note"].startswith("prior_range_unverified")


def test_the_item_dated_the_meeting_day_is_ignored_by_the_check(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.prior_in_feed = False
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = PRIOR_PAGE
    s, armed = _arm(tmp_ledger, net, clock)
    assert armed is None and tmp_ledger.release_get("fomc-2026-10")["note"].startswith("prior_range_unverified")
    assert not any(str(r.url) == LINK for _, r in net.reqs)                    # never even fetched


def test_a_link_on_another_host_is_ignored_by_the_check(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.prior_in_feed = False
    evil = "https://example.com/newsevents/pressreleases/monetary20260916a.htm"
    net.feed_xml = fed_feed([("FOMC statement", evil, "Wed, 16 Sep 2026 18:00:00 GMT")])
    net.pages[evil] = PRIOR_PAGE
    s, armed = _arm(tmp_ledger, net, clock)
    assert armed is None and tmp_ledger.release_get("fomc-2026-10")["note"].startswith("prior_range_unverified")
    assert not any(r.url.host == "example.com" for _, r in net.reqs)


def test_the_check_picks_the_newest_earlier_statement(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.prior_in_feed = False
    older = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm"
    net.feed_xml = fed_feed([("FOMC statement", older, "Wed, 29 Jul 2026 18:00:00 GMT"),
                             ("FOMC statement", PRIOR_LINK, "Wed, 16 Sep 2026 18:00:00 GMT")])
    net.pages[older] = "<p>maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent.</p>"
    s, armed = _arm(tmp_ledger, net, clock)
    assert armed is not None and "2026-09-16" in s.prior_check["fomc-2026-10"]      # Sep 16 says 3-3/4 to 4: the older item is not used


# ---------------------------------------------------------------- restart guard
@pytest.mark.parametrize("status", ["computed", "polling", "error"])
def test_a_started_fomc_release_is_not_rearmed_after_a_restart(tmp_ledger, status):
    clock = Clock(ts("2026-10-28", "13:59"))
    tmp_ledger.release_put(id="fomc-2026-10", kind="fomc", status=status)
    s = make(tmp_ledger, Net(clock), clock)
    assert s.refusal(FOMC) == "already_started"
    assert asyncio.run(s.arm(FOMC)) is None


def test_a_restart_inside_the_window_cannot_buy_twice(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = STATEMENT_CUT
    assert asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC)) == "done"
    assert trades
    clock2 = Clock(ts("2026-10-28", "14:00"))
    net2, again = Net(clock2), []
    net2.feed_xml = fed_feed([STMT_ITEM])
    net2.pages[LINK] = STATEMENT_CUT
    asyncio.run(make(tmp_ledger, net2, clock2, trades=again).run_release(FOMC))
    assert again == []


def test_a_crash_mid_release_leaves_it_already_started(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = STATEMENT_CUT
    s = sched_for(tmp_ledger, net, clock)

    async def boom(*a, **k):
        raise RuntimeError("crash while trading")
    s.trade = boom
    try:
        asyncio.run(s.run_release(FOMC))
    except RuntimeError:
        pass
    assert tmp_ledger.release_get("fomc-2026-10")["status"] in ("computed", "error")
    assert make(tmp_ledger, Net(clock), clock).refusal(FOMC) == "already_started"


def test_polling_is_written_before_the_first_poll_request(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = STATEMENT_CUT
    seen = []

    def spy(req):
        if req.url.host == FED:
            row = tmp_ledger.release_get("fomc-2026-10")
            seen.append(row["status"] if row else None)
        return None
    wrap(net, spy)
    asyncio.run(sched_for(tmp_ledger, net, clock).run_release(FOMC))
    first_poll = next(i for i, s_ in enumerate(seen) if s_ is not None)
    assert seen[first_poll] == "polling"
    assert seen[:first_poll] == [None, None]                 # only the two arm-time GETs (feed, prior statement) come earlier


def test_computed_is_written_before_the_first_trade(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = STATEMENT_CUT
    s = sched_for(tmp_ledger, net, clock)
    statuses = []

    async def trade(ev, market, side, bk, value, n_candidates=1, style=None):
        statuses.append(tmp_ledger.release_get("fomc-2026-10")["status"])
    s.trade = trade
    assert asyncio.run(s.run_release(FOMC)) == "done"
    assert statuses and set(statuses) == {"computed"}
    assert tmp_ledger.release_get("fomc-2026-10")["status"] == "done"


# ---------------------------------------------------------------- polling cadence and cap
def _poll(ledger, net, clock, entry=FOMC):
    s = sched_for(ledger, net, clock)

    async def go():
        try:
            return s, await s.poll_fomc(entry, T)
        finally:
            await s.http.aclose()
    return asyncio.run(go())


def test_requests_alternate_url_then_feed_with_half_second_then_two_second_gaps(tmp_ledger):
    clock = Clock(T - 5)
    net = Net(clock)
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    got = fed_gets(net)
    assert rng is None and text is None and timing["source"] is None
    kinds = ["url" if is_url(r) else "feed" if is_feed(r) else "other" for _, r in got]
    assert kinds[0::2] == ["url"] * (len(kinds) // 2) and kinds[1::2] == ["feed"] * (len(kinds) // 2)
    rounds = [t for (t, r) in got if is_url(r)]
    start = rounds[0]
    gaps = [(a - start, round(b - a, 6)) for a, b in zip(rounds, rounds[1:])]
    assert {g for off, g in gaps if off < 60.0} == {0.5} and {g for off, g in gaps if off >= 60.0} == {2.0}
    assert len([1 for off, g in gaps if off < 60.0]) == 120
    assert rounds[-1] - start < releases.FOMC_MAX_S
    assert len(got) <= releases.FOMC_MAX_REQUESTS and timing["requests"] == len(got) == 480


def test_statement_at_the_direct_url_is_used_and_a_404_is_silent(tmp_ledger, monkeypatch):
    clock = Clock(T - 5)
    net = Net(clock)
    net.pages[LINK] = STATEMENT_CUT
    wrap(net, lambda r: httpx.Response(404) if is_url(r) and clock.t < T + 1.0 else None)
    captured = []
    monkeypatch.setattr(releases.errors, "capture", lambda exc, where="": captured.append((exc, where)))
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    assert rng == (3.5, 3.75) and link == LINK and timing["source"] == "url"
    assert timing["seen_ts"] - T == pytest.approx(1.0, abs=0.5)
    assert captured == []                                              # the 404s were expected
    assert timing["polls"] >= 12 and timing["requests"] == len([r for r in net.all if r.url.host == FED])


def test_feed_only_availability_yields_source_feed(tmp_ledger):
    clock = Clock(T - 5)
    net = Net(clock)
    net.feed_xml = fed_feed([("FOMC statement", OTHER, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[OTHER] = STATEMENT_CUT
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    assert rng == (3.5, 3.75) and timing["source"] == "feed" and link == OTHER


def test_nothing_within_300_seconds_times_out_with_no_trade(tmp_ledger):
    clock = Clock(T - 5)
    net, trades = Net(clock), []
    s = sched_for(tmp_ledger, net, clock, trades=trades)
    assert asyncio.run(s.run_release(FOMC)) == "timed_out"
    row = tmp_ledger.release_get("fomc-2026-10")
    assert row["status"] == "timed_out" and trades == [] and row["note"] != "request_cap"


def test_request_cap_stops_the_loop_at_exactly_the_cap(tmp_ledger, monkeypatch):
    monkeypatch.setattr(releases, "FOMC_MAX_REQUESTS", 10)
    clock = Clock(ts("2026-10-28", "13:50"))
    net = Net(clock)
    s = sched_for(tmp_ledger, net, clock)
    assert asyncio.run(s.run_release(FOMC)) == "timed_out"
    assert tmp_ledger.release_get("fomc-2026-10")["note"] == "request_cap"
    polled = [r for t, r in fed_gets(net) if t >= T - releases.FOMC_LEAD_S - 1e-6 and (is_url(r) or is_feed(r))]
    assert len(polled) == 10


def test_a_failing_statement_url_is_captured_and_polling_continues(tmp_ledger, monkeypatch):
    clock = Clock(T - 5)
    net = Net(clock)
    net.feed_xml = fed_feed([("FOMC statement", OTHER, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[OTHER] = STATEMENT_CUT
    wrap(net, lambda r: httpx.Response(500) if is_url(r) else None)
    captured = []
    monkeypatch.setattr(releases.errors, "capture", lambda exc, where="": captured.append(where))
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    assert timing["source"] == "feed" and captured and captured[0] == "releases.fomc"


def test_etag_is_sent_back_and_a_304_is_not_a_failure(tmp_ledger):
    clock = Clock(T - 5)
    net = Net(clock)
    wrap(net, lambda r: httpx.Response(304) if is_feed(r) and r.headers.get("if-none-match") == "x" else None)
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    feeds = [r for r in net.all if r.url.host == FED and is_feed(r)]
    assert "if-none-match" not in feeds[0].headers and feeds[1].headers["if-none-match"] == "x"
    assert timing["source"] is None and all(r.headers.get("if-none-match") == "x" for r in feeds[1:])


def test_etag_is_not_stored_when_the_statement_page_failed(tmp_ledger):
    clock = Clock(T - 5)
    net = Net(clock)
    net.feed_xml = fed_feed([("FOMC statement", OTHER, "Wed, 28 Oct 2026 18:00:00 GMT")])
    pages = []

    def fn(r):
        if str(r.url) == OTHER:
            pages.append(clock.t)
            return httpx.Response(500) if len(pages) == 1 else httpx.Response(200, text=STATEMENT_CUT)
        if is_feed(r) and r.headers.get("if-none-match") == "x":
            return httpx.Response(304)
        return None
    wrap(net, fn)
    s, (rng, text, link, timing) = _poll(tmp_ledger, net, clock)
    assert len(pages) == 2 and timing["source"] == "feed"


# ---------------------------------------------------------------- timing, release_books, entry style
def _full_run(ledger, env=None, trade_ret=None, books=None, styles=None, mutate=None):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = STATEMENT_CUT
    if mutate:
        wrap(net, mutate)
    s = sched_for(ledger, net, clock, trades=trades, env=env, books=books, styles=styles)
    if trade_ret is not None:
        inner = s.trade

        async def trade(ev, market, side, bk, value, n_candidates=1, style=None):
            await inner(ev, market, side, bk, value, n_candidates=n_candidates, style=style)
            return trade_ret(market)
        s.trade = trade

    async def go():
        status = await s.run_release(FOMC)
        await asyncio.gather(*list(s._tasks), return_exceptions=True)
        await s.http.aclose()
        return status
    return asyncio.run(go()), trades, net, s


def test_raw_timing_fields_after_done_and_first_fill_from_the_trade(tmp_ledger):
    status, trades, net, s = _full_run(tmp_ledger, trade_ret=lambda m: {"fill_ts": T + 0.7})
    assert status == "done"
    raw = json.loads(tmp_ledger.release_get("fomc-2026-10")["raw"])
    for k in ("range", "prior_range", "url", "source", "statement_seen_ts", "seen_lag_s", "decided_ts", "decided_lag_s",
              "first_fill_ts", "first_fill_lag_s", "polls", "requests"):
        assert k in raw, k
    assert raw["first_fill_ts"] == T + 0.7 and raw["first_fill_lag_s"] == pytest.approx(0.7)
    assert raw["source"] == "url" and raw["polls"] >= 1 and raw["requests"] >= 1


def test_no_fill_leaves_first_fill_null(tmp_ledger):
    status, *_ = _full_run(tmp_ledger)
    raw = json.loads(tmp_ledger.release_get("fomc-2026-10")["raw"])
    assert status == "done" and raw["first_fill_ts"] is None and raw["first_fill_lag_s"] is None


def test_release_books_hold_decision_and_three_snapshots_for_all_five(tmp_ledger):
    status, trades, net, s = _full_run(tmp_ledger)
    rows = tmp_ledger.release_books("fomc-2026-10")
    by = {}
    for r in rows:
        by.setdefault(r["market_id"], set()).add(r["label"])
    assert set(by) == {"2589810", "2589811", "2589812", "2589813", "2589814"}
    assert all(v == {"decision", "+5s", "+30s", "+60s"} for v in by.values())
    dec = next(r for r in rows if r["label"] == "decision")
    assert dec["yes_ask"] == pytest.approx(.50) and dec["yes_bid"] == pytest.approx(.45) and dec["ask_qty"] == 100


def test_polymarket_trades_are_take_and_kalshi_trades_pass_no_style(tmp_ledger):
    styles = []
    status, *_ = _full_run(tmp_ledger, styles=styles)
    poly = [st for src, st in styles if src == "release:POLYFED"]
    kal = [st for src, st in styles if src != "release:POLYFED"]
    assert status == "done" and poly and set(poly) == {"take"} and kal and set(kal) == {None}


def _poly(trades):
    return [(t[1]["id"], t[2]) for t in trades if t[0]["source"] == "release:POLYFED"]


def test_done_note_counts_traded_and_skipped(tmp_ledger):
    status, trades, net, s = _full_run(tmp_ledger)
    assert tmp_ledger.release_get("fomc-2026-10")["note"] == f"{len(trades)} traded, 0 skipped"


def test_poly_max_no_zero_buys_yes_only(tmp_ledger):
    status, trades, *_ = _full_run(tmp_ledger, env={"POLY_FOMC_MAX_NO": "0"})
    assert _poly(trades) == [("2589811", "yes")]


def test_default_buys_the_yes_bracket_then_one_dead_no(tmp_ledger):
    status, trades, *_ = _full_run(tmp_ledger)
    assert _poly(trades) == [("2589811", "yes"), ("2589810", "no")]
    ids = [t[0]["id"] for t in trades if t[0]["source"] == "release:POLYFED"]
    assert ids == ["release-POLYFED-2026-10", "release-POLYFED-2026-10-m1"]


@pytest.mark.parametrize("flag", [{"acceptingOrders": False}, {"closed": True}, {"active": False}])
def test_yes_bracket_no_longer_open_is_not_traded(tmp_ledger, flag):
    base = json.loads((POLY / "markets" / "2589811.json").read_text())

    def mutate(r):
        if r.url.host == "gamma-api.polymarket.com" and r.url.path == "/markets/2589811":
            return httpx.Response(200, json={**base, **flag})
    status, trades, *_ = _full_run(tmp_ledger, mutate=mutate)
    assert status == "done" and _poly(trades) == [("2589810", "no")]
    assert tmp_ledger.release_get("fomc-2026-10")["note"].endswith("1 skipped")


@pytest.mark.parametrize("kind", ["empty", "no_bid", "no_ask"])
def test_fresh_book_that_degrades_before_the_order_is_skipped(tmp_ledger, kind):
    state = {"after": False}

    def mutate(r):
        if r.url.host == "gamma-api.polymarket.com" and r.url.path == "/markets/2589811":
            state["after"] = True               # the re-check runs right before the fresh book is fetched
        return None

    def books(mid):
        if mid == "2589811" and state["after"]:
            if kind == "empty":
                return Book("polymarket", mid, [], [])
            if kind == "no_bid":
                return Book("polymarket", mid, [(.50, 100)], [])         # YES ask but no NO ask: no YES bid
            return Book("polymarket", mid, [], [(.55, 100)])             # no YES ask at all
        return Book("polymarket", mid, [(.50, 100)], [(.55, 100)])
    status, trades, *_ = _full_run(tmp_ledger, books=books, mutate=mutate)
    assert _poly(trades) == [("2589810", "no")]
    assert tmp_ledger.release_get("fomc-2026-10")["note"].endswith("1 skipped")


def test_empty_books_everywhere_mean_no_polymarket_trade(tmp_ledger):
    def books(mid):
        if mid.isdigit():
            return Book("polymarket", mid, [], [])
        return Book("kalshi", mid, [(.50, 100)], [(.55, 100)])
    status, trades, *_ = _full_run(tmp_ledger, books=books)
    assert status == "done" and _poly(trades) == []


def test_poly_disabled_makes_no_gamma_request_and_no_polyfed_trade(tmp_ledger):
    status, trades, net, s = _full_run(tmp_ledger, env={"POLY_FOMC_ENABLED": "false"})
    assert _poly(trades) == [] and not any(r.url.host == "gamma-api.polymarket.com" for _, r in net.reqs)
    assert {t[0]["source"] for t in trades} == {"release:KXFED", "release:KXFEDDECISION"}


def test_all_requests_of_a_full_release_are_get(tmp_ledger):
    status, trades, net, s = _full_run(tmp_ledger)
    assert {r.method for _, r in net.reqs} == {"GET"}


# ---------------------------------------------------------------- v0.5.0 guards on the Polymarket path
def _hold_run(ledger, body):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([STMT_ITEM])
    net.pages[LINK] = f"<html><body><p>{body}</p></body></html>"
    status = asyncio.run(make(ledger, net, clock, trades=trades).run_release(FOMC))
    return status, trades


@pytest.mark.parametrize("body", [
    "lower the target range for the federal funds rate by 3/4 percentage point to 3 to 3-1/4 percent.",   # 75 bp cut
    "raise the target range for the federal funds rate by 1/8 percentage point to 4 to 4-1/8 percent.",   # 12.5 bp
    "Nothing parseable here."])
def test_non_25_moves_and_unparseable_statements_trade_nothing_on_any_venue(tmp_ledger, body):
    status, trades = _hold_run(tmp_ledger, body)
    assert status == "parse_doubt" and trades == []


@pytest.mark.parametrize("body,want", [
    ("maintain the target range for the federal funds rate at 3-3/4 to 4 percent.", ("2589812", "yes")),
    ("raise the target range for the federal funds rate by 1/4 percentage point to 4 to 4-1/4 percent.", ("2589813", "yes")),
    ("lower the target range for the federal funds rate by 1/2 percentage point to 3-1/4 to 3-1/2 percent.", ("2589810", "yes")),
])
def test_each_decision_buys_yes_on_its_bracket(tmp_ledger, body, want):
    status, trades = _hold_run(tmp_ledger, body)
    assert status == "done" and _poly(trades)[0] == want


def test_a_statement_from_range_that_disagrees_trades_nothing_on_polymarket(tmp_ledger):
    status, trades = _hold_run(tmp_ledger, "lower the target range for the federal funds rate by 1/4 percentage point to "
                                           "3-1/2 to 3-3/4 percent, from 4-1/4 to 4-1/2 percent.")
    assert status == "parse_doubt" and _poly(trades) == []


# ---------------------------------------------------------------- --check
def test_check_prints_the_polyfed_table_and_prior_range_line_get_only(tmp_path, tmp_ledger):
    out, net = [], Net()
    cfg = releases.settings({})
    cal = write_cal(tmp_path, [{"kind": "fomc", "date": "2026-10-28", "time_et": "14:00", "period": "2026-10",
                                "prior_range": [3.75, 4.0]}])

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
        n = await releases.check("2026-10-28", cfg, calendar_path=cal, ledger=tmp_ledger, http=http, out=out.append)
        await http.aclose()
        return n
    n = asyncio.run(go())
    text = "\n".join(out)
    assert n == len(net.reqs) and {r.method for _, r in net.reqs} == {"GET"}
    assert "POLYFED event: fed-decision-in-october" in text
    for mid, bracket in [("2589810", "cut_50"), ("2589811", "cut_25"), ("2589812", "hold"), ("2589813", "hike_25"),
                         ("2589814", "hike_50")]:
        assert mid in text and bracket in text
    assert "acceptingOrders true" in text and "prior_range check: prior_range 3.75-4 verified" in text
    assert f"GET requests sent: {n}" in text
