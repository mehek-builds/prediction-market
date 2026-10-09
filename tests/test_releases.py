"""Scheduled data releases: calendar gating, rules templates against captured Kalshi fixtures, BLS v2 budget and polling,
rounding margins, FOMC parsing, ranking, and trade_release through the engine. Offline: httpx.MockTransport, fake clock."""
import asyncio
import copy
import dataclasses
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from fastlane import engine as engine_mod
from fastlane import live, releases
from fastlane.books import Book
from fastlane.releases import (CalendarEntry, ParsedMarket, Point, ReleaseScheduler, compute_stat, fomc_range, load_calendar,
                               margin_ok, on_boundary, parse_bls, parse_market, planned_requests, resolves_yes,
                               rounds_safely, settled, today_entries)

from test_engine_handle import _make_engine
from test_live import FILLED, Recorder
from test_live import _engine as _live_engine

ET = ZoneInfo("America/New_York")
FIX = Path(__file__).parent / "fixtures" / "release_rules"
SERIES = ["KXCPI", "KXCPIYOY", "KXPAYROLLS", "KXU3", "KXFED", "KXFEDDECISION"]


def ts(day, hhmm):
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET).timestamp()


def fx(series):
    return copy.deepcopy(json.loads((FIX / f"{series}.json").read_text())["markets"])


CPI = CalendarEntry("cpi", "2026-10-14", "08:30", "2026-09")
JOBS = CalendarEntry("jobs", "2026-11-06", "08:30", "2026-10")
FOMC = CalendarEntry("fomc", "2026-10-28", "14:00", "2026-10", prior_range=(3.75, 4.0))
T_CPI = ts("2026-10-14", "08:30")


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += max(s, 0)


def bls_payload(rows):
    """rows: [(year, 'M09', '301.2')] newest first."""
    return {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"data": [
        {"year": str(y), "period": p, "periodName": "x", "value": v, "footnotes": [{}]} for y, p, v in rows]}]}}


class Net:
    """One MockTransport for Kalshi markets, BLS and the Fed. `bls(request) -> payload` and `fed_feed`/`pages` are set per test."""
    def __init__(self, clock=None, mutate=None, keep_close=False, only=None):
        self.clock, self.mutate, self.reqs = clock, mutate or {}, []
        self.keep_close, self.only = keep_close, only      # keep_close: serve the captured close times (they precede the release)
        self.bls = lambda req: bls_payload([])
        self.feed_xml = b"<rss version='2.0'><channel></channel></rss>"
        self.pages = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.reqs.append((self.clock.t if self.clock else None, req))
        if req.url.host == "api.bls.gov":
            return httpx.Response(200, json=self.bls(req))
        if req.url.path.endswith("/markets"):
            s = req.url.params["series_ticker"]
            ms = fx(s) if self.only in (None, s) else []
            if not self.keep_close:                  # most tests are about everything else: markets stay open past the release
                for m in ms:
                    m["close_time"] = "2099-01-01T00:00:00Z"
            if s in self.mutate:
                ms = self.mutate[s](ms)
            return httpx.Response(200, json={"markets": ms, "cursor": ""})
        if req.url.path.endswith("press_monetary.xml"):
            return httpx.Response(200, content=self.feed_xml, headers={"etag": "x"})
        if str(req.url) in self.pages:
            return httpx.Response(200, text=self.pages[str(req.url)])
        return httpx.Response(404)

    def of(self, host):
        return [r for _, r in self.reqs if r.url.host == host]


def make(ledger, net, clock, env=None, cal=None, trades=None, books=None, quote_wait=0.05):
    cfg = releases.settings({"BLS_API_KEY": "k", **(env or {})})
    http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))

    async def trade(ev, market, side, bk, value, n_candidates=1):
        (trades if trades is not None else []).append((ev, market, side, bk, value))

    async def fetch(_http, market):
        return books(market["id"]) if books else Book("kalshi", market["id"], [(.50, 100)], [(.55, 100)])   # YES room .50 beats NO room .45
    return ReleaseScheduler(ledger, http, trade, fetch, cfg=cfg, calendar_path=cal or Path("/nonexistent"), now=clock,
                            sleep=clock.sleep, quote_wait_s=quote_wait, verbose=False)


def write_cal(tmp_path, events, name="cal.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"version": 1, "todo": "x", "events": events}))
    return p


# ---------------------------------------------------------------- settings
def test_settings_defaults_and_overrides():
    s = releases.settings({})
    assert (s.enabled, s.bls_key, s.poll_start_s, s.poll_every_s, s.poll_max_s, s.daily_budget, s.margin_cpi_pp,
            s.margin_payrolls_k, s.max_markets_per_series) == (True, "", 2.0, 1.5, 90.0, 400, 0.02, 10.0, 1)
    s = releases.settings({"RELEASES_ENABLED": "false", "BLS_API_KEY": " abc ", "RELEASE_POLL_EVERY_S": "0.2",
                           "RELEASE_BLS_DAILY_BUDGET": "50"})
    assert s.enabled is False and s.bls_key == "abc" and s.poll_every_s == 1.0 and s.daily_budget == 50
    assert "abc" not in repr(s)                              # the key never lands in a log line


def test_settings_malformed_number_raises():
    with pytest.raises(ValueError, match="RELEASE_POLL_MAX_S"):
        releases.settings({"RELEASE_POLL_MAX_S": "soon"})


def test_planned_requests_at_defaults_is_122_for_bls_kinds():
    s = releases.settings({})
    assert planned_requests("cpi", s) == 122 and planned_requests("jobs", s) == 122 and planned_requests("fomc", s) == 0


# ---------------------------------------------------------------- calendar
def test_committed_calendar_holds_the_official_bls_dates():
    cal = load_calendar()
    cpi = {e.date: e.period for e in cal if e.kind == "cpi"}
    jobs = {e.date: e.period for e in cal if e.kind == "jobs"}
    assert len(cpi) == 14 and len(jobs) == 14
    assert cpi["2026-11-10"] == "2026-10" and cpi["2027-12-10"] == "2027-11" and jobs["2026-11-06"] == "2026-10"
    assert jobs["2027-12-03"] == "2027-11"
    assert "2026-10-14" not in cpi and not any(e.period == "2026-09" for e in cal if e.kind == "cpi")   # no September CPI date on bls.gov
    assert all(e.time_et == "08:30" for e in cal if e.kind in ("cpi", "jobs"))
    data = json.loads(releases.CALENDAR_PATH.read_text())
    assert "todo" not in data and data["sources"]["cpi"].startswith("https://www.bls.gov")
    assert "2026-10-10" in json.dumps(data["notes"])         # fetch date recorded


def test_empty_calendar_never_arms_or_requests(tmp_path, tmp_ledger):
    clock = Clock(T_CPI)
    net = Net(clock)
    s = make(tmp_ledger, net, clock, cal=write_cal(tmp_path, []))

    async def go():
        gap = await s.tick()
        assert gap == releases.IDLE_S and s.started == set() and not s._tasks
        await s.http.aclose()
    asyncio.run(go())
    assert net.reqs == [] and s.requests == 0


def test_malformed_entry_names_its_index(tmp_path):
    good = {"kind": "cpi", "date": "2026-10-14", "time_et": "08:30", "period": "2026-09"}
    for bad, msg in [({**good, "kind": "gdp"}, "events[1]"), ({**good, "date": "2026-13-01"}, "events[1]"),
                     ({**good, "time_et": "8:30"}, "events[1]"), ({**good, "period": "2026-9"}, "events[1]"),
                     ({**good, "prior_range": [3.75, 4.0]}, "fomc entries only"),
                     ({**good, "kind": "fomc", "prior_range": [4.0, 3.75]}, "prior_range"),
                     ({**good, "extra": 1}, "unknown keys"), ("nope", "events[1]")]:
        with pytest.raises(ValueError, match="events\\[1\\]|fomc entries|prior_range|unknown keys") as ei:
            load_calendar(write_cal(tmp_path, [good, bad]))
        assert msg in str(ei.value)
    load_calendar(write_cal(tmp_path, [good, {**good, "kind": "fomc", "prior_range": [3.75, 4.0]}]))


def test_calendar_file_errors_are_value_errors(tmp_path):
    with pytest.raises(ValueError):
        load_calendar(tmp_path / "missing.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_calendar(p)
    p.write_text('{"events": {}}')
    with pytest.raises(ValueError, match="events"):
        load_calendar(p)


def test_today_entries_only_returns_the_et_date(tmp_path):
    cal = load_calendar(write_cal(tmp_path, [
        {"kind": "cpi", "date": "2026-10-14", "time_et": "08:30", "period": "2026-09"},
        {"kind": "jobs", "date": "2026-10-15", "time_et": "08:30", "period": "2026-09"}]))
    assert [e.kind for e in today_entries(cal, ts("2026-10-14", "23:59"), ET)] == ["cpi"]
    assert [e.kind for e in today_entries(cal, ts("2026-10-15", "00:01"), ET)] == ["jobs"]


def test_tick_starts_only_todays_due_entries(tmp_path, tmp_ledger, monkeypatch):
    cal = write_cal(tmp_path, [
        {"kind": "cpi", "date": "2026-10-14", "time_et": "08:30", "period": "2026-09"},
        {"kind": "jobs", "date": "2026-10-15", "time_et": "08:30", "period": "2026-09"}])
    started = []

    async def fake_run(self_, entry):
        started.append(entry.release_id)
        return "x"

    async def go(now):
        clock = Clock(now)
        s = make(tmp_ledger, Net(clock), clock, cal=cal)
        monkeypatch.setattr(ReleaseScheduler, "run_release", fake_run)
        gap = await s.tick()
        await asyncio.gather(*list(s._tasks))
        await s.http.aclose()
        return gap
    gap = asyncio.run(go(T_CPI - 3600))                     # an hour early: nothing starts, sleep until T - 120 s
    assert started == [] and 0.5 <= gap <= releases.IDLE_S
    asyncio.run(go(T_CPI - 100))                            # inside the lead window, on the date
    assert started == ["cpi-2026-09"]                       # tomorrow's jobs entry is not started


# ---------------------------------------------------------------- arming
def test_arm_cpi_today_with_key(tmp_path, tmp_ledger):
    clock = Clock(T_CPI - 100)
    net = Net(clock)
    s = make(tmp_ledger, net, clock)
    armed = asyncio.run(s.arm(CPI))
    assert armed is not None and armed.planned == 122 and armed.scheduled_ts == T_CPI
    assert {k: len(v) for k, v in armed.markets.items()} == {"KXCPI": 14, "KXCPIYOY": 21}   # only the September markets
    assert all(r.method == "GET" for _, r in net.reqs)
    rows = tmp_ledger.db.execute("SELECT COUNT(*), SUM(template IS NOT NULL) FROM release_markets").fetchone()
    assert rows[0] == 35 and rows[1] == 35
    assert tmp_ledger.release_get("cpi-2026-09")["status"] == "armed"


def test_arm_refuses_other_day_missing_key_and_budget(tmp_ledger):
    clock = Clock(T_CPI - 100)
    net = Net(clock)
    assert asyncio.run(make(tmp_ledger, net, Clock(ts("2026-10-13", "09:00"))).arm(CPI)) is None
    assert asyncio.run(make(tmp_ledger, net, clock, env={"BLS_API_KEY": ""}).arm(CPI)) is None
    s = make(tmp_ledger, net, clock, env={"RELEASE_BLS_DAILY_BUDGET": "10"})
    assert s.refusal(CPI).startswith("budget") and asyncio.run(s.arm(CPI)) is None
    assert net.reqs == []                                    # every refusal happened before any request


def test_arm_refuses_when_the_days_usage_leaves_no_headroom(tmp_ledger):
    clock = Clock(T_CPI - 100)
    day = releases.utc_day(clock.t)
    for _ in range(100):
        tmp_ledger.bls_request_add(day)
    s = make(tmp_ledger, Net(clock), clock, env={"RELEASE_BLS_DAILY_BUDGET": "200"})
    assert s.refusal(CPI).startswith("budget")               # 100 used + 122 planned > 200
    s2 = make(tmp_ledger, Net(clock), clock, env={"RELEASE_BLS_DAILY_BUDGET": "222"})
    assert s2.refusal(CPI) is None                           # exactly enough headroom


def test_fomc_and_jobs_kinds_do_not_need_a_key_for_fomc_only(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    s = make(tmp_ledger, Net(clock), clock, env={"BLS_API_KEY": ""})
    assert s.refusal(FOMC) is None
    clock2 = Clock(ts("2026-11-06", "08:20"))
    assert make(tmp_ledger, Net(clock2), clock2, env={"BLS_API_KEY": ""}).refusal(JOBS) == "no_bls_key"


H1_CASES = [("KXCPI", CPI), ("KXCPIYOY", CPI), ("KXPAYROLLS", JOBS), ("KXU3", JOBS),
            ("KXFED", FOMC), ("KXFEDDECISION", FOMC)]


@pytest.mark.parametrize("series,entry", H1_CASES)
def test_series_closing_before_the_release_never_arms(tmp_ledger, series, entry):
    """Captured close times: KXCPI 08:25 ET, KXCPIYOY/KXPAYROLLS/KXU3 08:29 ET, KXFED 13:55 ET, KXFEDDECISION 13:59 ET."""
    clock = Clock(entry.scheduled_ts(releases.ZoneInfo("America/New_York")) - 100)
    net = Net(clock, keep_close=True, only=series)
    s = make(tmp_ledger, net, clock)
    assert asyncio.run(s.arm(entry)) is None
    row = tmp_ledger.release_get(entry.release_id)
    assert row["status"] == "not_armed" and row["note"] == "markets_close_before_release"


def test_a_market_closing_after_the_release_still_arms(tmp_ledger):
    clock = Clock(T_CPI - 100)

    def later(ms):
        for m in ms:
            m["close_time"] = "2026-10-14T13:00:00Z" if m["ticker"].endswith("-T0.3") else m["close_time"]
        return ms
    net = Net(clock, keep_close=True, mutate={"KXCPI": later})
    armed = asyncio.run(make(tmp_ledger, net, clock).arm(CPI))
    assert armed is not None and armed.markets["KXCPIYOY"] == []
    assert [pm.ticker for pm in armed.markets["KXCPI"]] and all(pm.ticker.endswith("-T0.3") for pm in armed.markets["KXCPI"])


def test_a_market_that_closes_before_the_trade_is_not_traded(tmp_ledger):
    clock = Clock(T_CPI - 300)
    trades = []
    s = make(tmp_ledger, _cpi_net(clock), clock, trades=trades)

    async def go():
        armed = await s.arm(CPI)
        assert armed is not None
        clock.t = T_CPI + 1
        for pms in armed.markets.values():            # every market closes just after arming
            for i, pm in enumerate(pms):
                pms[i] = dataclasses.replace(pm, close_time="2026-10-14T12:30:00Z")
        return await s._trade_values(armed, {"KXCPI": 0.4, "KXCPIYOY": 3.0}, clock.t, "{}")
    assert asyncio.run(go()) == 0 and trades == []


def test_arm_without_matching_markets_does_not_arm(tmp_ledger):
    clock = Clock(T_CPI - 100)
    e = CalendarEntry("cpi", "2026-10-14", "08:30", "2027-05")       # no fixture market names May 2027
    s = make(tmp_ledger, Net(clock), clock)
    assert asyncio.run(s.arm(e)) is None
    assert tmp_ledger.release_get("cpi-2027-05")["status"] == "not_armed"


@pytest.mark.parametrize("status", ["computed", "polling", "error"])
def test_a_release_that_already_started_is_not_rearmed_after_a_restart(tmp_ledger, status):
    clock = Clock(T_CPI - 100)
    tmp_ledger.release_put(id="cpi-2026-09", kind="cpi", status=status)
    assert make(tmp_ledger, Net(clock), clock).refusal(CPI) == "already_started"
    tmp_ledger.release_put(id="cpi-2026-09", kind="cpi", status="timed_out")
    assert make(tmp_ledger, Net(clock), clock).refusal(CPI) is None          # a clean failure may be retried


def test_a_done_release_is_never_rearmed(tmp_ledger):
    clock = Clock(T_CPI - 100)
    tmp_ledger.release_put(id="cpi-2026-09", kind="cpi", status="done")
    assert make(tmp_ledger, Net(clock), clock).refusal(CPI) == "already_done"


def test_releases_disabled_refuses(tmp_ledger):
    clock = Clock(T_CPI - 100)
    assert make(tmp_ledger, Net(clock), clock, env={"RELEASES_ENABLED": "false"}).refusal(CPI) == "releases_disabled"


def test_fomc_decision_series_needs_prior_range(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    no_prior = CalendarEntry("fomc", "2026-10-28", "14:00", "2026-10")
    a = asyncio.run(make(tmp_ledger, Net(clock), clock).arm(no_prior))
    assert a.markets["KXFEDDECISION"] == [] and len(a.markets["KXFED"]) == 11
    b = asyncio.run(make(tmp_ledger, Net(clock), clock).arm(FOMC))
    assert len(b.markets["KXFEDDECISION"]) == 5


def test_a_changed_rules_text_is_stored_as_mismatch_and_not_traded(tmp_ledger):
    clock = Clock(ts("2026-11-06", "08:20"))

    def tamper(ms):
        for m in ms:
            if m["ticker"] == "KXU3-26OCT-T4.0":
                m["rules_primary"] = m["rules_primary"].replace("seasonally adjusted", "not seasonally adjusted")
        return ms
    s = make(tmp_ledger, Net(clock, {"KXU3": tamper}), clock)
    armed = asyncio.run(s.arm(JOBS))
    assert "KXU3-26OCT-T4.0" not in [m.ticker for m in armed.markets["KXU3"]]
    note = tmp_ledger.db.execute("SELECT template, note FROM release_markets WHERE market_id='KXU3-26OCT-T4.0'").fetchone()
    assert note == (None, "rules_mismatch")


# ---------------------------------------------------------------- BLS v2 key, budget, polling
def test_bls_requests_use_v2_with_the_key_one_series_per_get(tmp_ledger):
    clock = Clock(T_CPI - 60)
    net = Net(clock)
    net.bls = lambda req: bls_payload([(2026, "M08", "300.0"), (2025, "M09", "292.0")])
    s = make(tmp_ledger, net, clock)
    base = asyncio.run(s.baseline(CPI))
    assert set(base) == {"CUSR0000SA0", "CUUR0000SA0"}
    reqs = net.of("api.bls.gov")
    assert len(reqs) == 2 and {r.method for r in reqs} == {"GET"}
    for r in reqs:
        assert r.url.path.startswith("/publicAPI/v2/timeseries/data/") and "/v1/" not in r.url.path
        assert r.url.params["registrationkey"] == "k" and r.url.params["startyear"] == "2025" and r.url.params["endyear"] == "2026"
    assert {r.url.path.rsplit("/", 1)[1] for r in reqs} == {"CUSR0000SA0", "CUUR0000SA0"}
    assert tmp_ledger.bls_requests(releases.utc_day(clock.t)) == 2


def test_no_key_means_zero_bls_requests_even_with_an_entry(tmp_ledger):
    clock = Clock(T_CPI - 100)
    net = Net(clock)
    s = make(tmp_ledger, net, clock, env={"BLS_API_KEY": ""})
    assert asyncio.run(s.run_release(CPI)) == "not_armed"
    assert net.reqs == []


def test_count_is_incremented_before_the_send_and_a_failed_send_still_counts(tmp_ledger):
    clock = Clock(T_CPI)
    seen = []

    def boom(req):
        seen.append(tmp_ledger.bls_requests(releases.utc_day(clock.t)))    # what the ledger says while the request is in flight
        raise httpx.ConnectError("down")
    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    s = ReleaseScheduler(tmp_ledger, http, None, None, cfg=releases.settings({"BLS_API_KEY": "secret-key"}), now=clock,
                         sleep=clock.sleep, verbose=False)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(s.bls_get("CUSR0000SA0", latest="true"))
    assert seen == [1] and tmp_ledger.bls_requests(releases.utc_day(clock.t)) == 1
    assert "secret-key" not in str(ei.value) and "registrationkey" not in str(ei.value)


def test_bls_get_raises_budget_hit_without_sending(tmp_ledger):
    clock = Clock(T_CPI)
    net = Net(clock)
    s = make(tmp_ledger, net, clock, env={"RELEASE_BLS_DAILY_BUDGET": "0"})
    with pytest.raises(releases.BudgetHit):
        asyncio.run(s.bls_get("CUSR0000SA0"))
    assert net.reqs == []


def test_poll_loop_stops_at_the_budget_with_budget_hit(tmp_ledger):
    clock = Clock(T_CPI - 2)
    net = Net(clock)
    net.bls = lambda req: bls_payload([(2026, "M08", "300.0")])          # never a new period
    s = make(tmp_ledger, net, clock, env={"RELEASE_BLS_DAILY_BUDGET": "7"})
    base = {"CUSR0000SA0": [Point(2026, 8, 300.0)], "CUUR0000SA0": [Point(2026, 8, 299.0)]}
    new, status = asyncio.run(s.poll_bls(CPI, base, T_CPI))
    assert status == "budget_hit" and new == {}
    assert len(net.of("api.bls.gov")) == 7 and tmp_ledger.bls_requests(releases.utc_day(clock.t)) == 7


def test_poll_schedule_first_poll_at_once_then_every_1_5_s_stop_at_first_newer_period(tmp_ledger):
    clock = Clock(T_CPI - 10)
    net = Net(clock)
    calls = {"n": 0}

    def serve(req):
        sid = req.url.path.rsplit("/", 1)[1]
        calls["n"] += 1
        old = [(2026, "M08", "300.0")]
        # the SA series shows September on its 2nd poll, the NSA series on its 3rd
        n = sum(1 for _, r in net.reqs if r.url.path.endswith(sid))
        if sid == "CUSR0000SA0" and n >= 2:
            return bls_payload([(2026, "M09", "301.2")] + old)
        if sid == "CUUR0000SA0" and n >= 3:
            return bls_payload([(2026, "M09", "300.76")] + old)
        return bls_payload(old)
    net.bls = serve
    s = make(tmp_ledger, net, clock)
    base = {"CUSR0000SA0": [Point(2026, 8, 300.0)], "CUUR0000SA0": [Point(2026, 8, 300.0)]}
    new, status = asyncio.run(s.poll_bls(CPI, base, T_CPI))
    assert status == "ok" and set(new) == {"CUSR0000SA0", "CUUR0000SA0"}
    times = [t - T_CPI for t, r in net.reqs]
    assert times == pytest.approx([-2, -2, -0.5, -0.5, 1.0])      # T-2 both series, then +1.5 s per round; SA done after round 2
    assert len(net.of("api.bls.gov")) == 5


def test_poll_times_out_after_poll_max_and_ignores_m13(tmp_ledger):
    clock = Clock(T_CPI - 2)
    net = Net(clock)
    net.bls = lambda req: bls_payload([(2026, "M13", "310.0"), (2026, "M08", "300.0")])   # annual average is not a new month
    s = make(tmp_ledger, net, clock)
    base = {"CUSR0000SA0": [Point(2026, 8, 300.0)], "CUUR0000SA0": [Point(2026, 8, 300.0)]}
    new, status = asyncio.run(s.poll_bls(CPI, base, T_CPI))
    assert status == "timed_out" and new == {}
    last = net.reqs[-1][0] - (T_CPI - 2)
    assert 87 <= last <= 90                                             # stops once 90 s have passed
    assert len(net.of("api.bls.gov")) == 120                            # 60 rounds x 2 series


def test_parse_bls_ignores_everything_that_is_not_a_clean_success():
    assert parse_bls({"status": "REQUEST_NOT_PROCESSED"}) == []
    assert parse_bls({}) == [] and parse_bls(None) == [] and parse_bls([]) == []
    good = bls_payload([(2026, "M13", "1"), (2026, "M09", "5"), (2026, "S01", "3"), (2025, "M12", "4")])
    assert [(p.year, p.month, p.value) for p in parse_bls(good)] == [(2026, 9, 5.0), (2025, 12, 4.0)]
    prelim = {"status": "REQUEST_SUCCEEDED", "Results": {"series": [{"data": [
        {"year": "2026", "period": "M09", "value": "1,234.5", "footnotes": [{"code": "P"}]}]}]}}
    p = parse_bls(prelim)[0]
    assert p.preliminary and p.value == 1234.5


# ---------------------------------------------------------------- computation and rounding
def test_cpi_mom_from_two_sa_indexes_and_yoy_from_nsa_thirteen_months_apart():
    base = {"CUSR0000SA0": [Point(2026, 8, 300.0)], "CUUR0000SA0": [Point(2025, 9, 292.0), Point(2026, 8, 299.0)]}
    new = {"CUSR0000SA0": [Point(2026, 9, 301.2), Point(2026, 8, 300.0)], "CUUR0000SA0": [Point(2026, 9, 300.76), Point(2025, 9, 292.0)]}
    raw, val = compute_stat("KXCPI", (2026, 9), base, new)
    assert raw == pytest.approx(0.4) and val == 0.4
    raw, val = compute_stat("KXCPIYOY", (2026, 9), base, new)
    assert raw == pytest.approx(3.0) and val == 3.0


def test_cpi_mom_uses_the_revised_prior_month_from_the_new_response_not_the_baseline():
    # January data: BLS revises the seasonally adjusted history. Baseline Dec = 300.0, revised Dec = 300.6, Jan = 301.5.
    base = {"CUSR0000SA0": [Point(2026, 12, 300.0)]}
    new = {"CUSR0000SA0": [Point(2027, 1, 301.5), Point(2026, 12, 300.6)]}
    raw, val = compute_stat("KXCPI", (2027, 1), base, new)
    assert raw == pytest.approx((301.5 / 300.6 - 1) * 100) and val == 0.3          # the stale base would give 0.5
    assert compute_stat("KXCPI", (2027, 1), base, {"CUSR0000SA0": [Point(2027, 1, 301.5)]}) is None   # no prior in the response


def test_cpi_polls_ask_for_the_year_range_not_latest(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net = _cpi_net(clock)
    asyncio.run(make(tmp_ledger, net, clock).run_release(CPI))
    reqs = net.of("api.bls.gov")
    assert reqs and all("latest" not in r.url.params and r.url.params["startyear"] == "2025"
                        and r.url.params["endyear"] == "2026" for r in reqs)


def test_release_run_uses_the_revised_prior_month_from_the_poll_response(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net = Net(clock)

    def serve(req):
        sid = req.url.path.rsplit("/", 1)[1]
        if clock.t < T_CPI:
            return bls_payload([(2026, "M08", "300.0")] if sid == "CUSR0000SA0" else [(2026, "M08", "299.0"), (2025, "M09", "292.0")])
        if sid == "CUSR0000SA0":
            return bls_payload([(2026, "M09", "301.5"), (2026, "M08", "300.6")])      # August revised from 300.0
        return bls_payload([(2026, "M09", "300.76"), (2026, "M08", "299.0"), (2025, "M09", "292.0")])
    net.bls = serve
    assert asyncio.run(make(tmp_ledger, net, clock).run_release(CPI)) == "done"
    assert json.loads(tmp_ledger.release_get("cpi-2026-09")["raw"])["KXCPI"]["settled"] == 0.3    # stale baseline: 0.5


def test_cpi_inputs_missing_gives_none():
    new = {"CUSR0000SA0": [Point(2026, 9, 301.2)]}
    assert compute_stat("KXCPI", (2026, 9), {"CUSR0000SA0": []}, new) is None
    assert compute_stat("KXCPI", (2026, 10), {"CUSR0000SA0": [Point(2026, 9, 1.0)]}, new) is None


def test_payrolls_difference_uses_the_revised_prior_month_from_the_same_response():
    new = {"CES0000000001": [Point(2026, 10, 159500.0), Point(2026, 9, 159345.0), Point(2026, 8, 159000.0)]}
    raw, val = compute_stat("KXPAYROLLS", (2026, 10), {}, new)
    assert raw == 155.0 and val == 155.0
    assert compute_stat("KXPAYROLLS", (2026, 10), {}, {"CES0000000001": [Point(2026, 10, 1.0)]}) is None


def test_unemployment_is_passed_through_as_published():
    raw, val = compute_stat("KXU3", (2026, 10), {}, {"LNS14000000": [Point(2026, 10, 4.3)]})
    assert raw == 4.3 and val == 4.3


@pytest.mark.parametrize("v,want", [(0.25, 0.3), (0.35, 0.4), (0.15, 0.2), (-0.25, -0.3), (-0.35, -0.4), (0.24, 0.2), (3.0, 3.0)])
def test_settled_rounds_half_away_from_zero(v, want):
    assert settled(v, 1) == want


def test_settled_whole_numbers():
    assert settled(155.5, 0) == 156.0 and settled(-155.5, 0) == -156.0


@pytest.mark.parametrize("v,ok", [(0.34, False), (0.32, True), (0.35, False), (0.30, True), (0.36, False), (0.33, True),
                                  (0.37, True), (-0.35, False), (-0.30, True), (3.04, False), (3.02, True), (3.05, False)])
def test_rounds_safely_margin_at_exact_boundaries(v, ok):
    assert rounds_safely(v, 1, 0.02) is ok


def test_rounds_safely_zero_margin_and_other_decimals():
    assert rounds_safely(0.35, 1, 0.0) is True
    assert rounds_safely(1.005, 2, 0.002) is False and rounds_safely(1.002, 2, 0.002) is True


def payroll_market(strike_persons):
    m = fx("KXPAYROLLS")[2]
    m = {**m, "ticker": f"KXPAYROLLS-26OCT-T{strike_persons}", "floor_strike": strike_persons,
         "rules_primary": m["rules_primary"].replace("above 10000", f"above {strike_persons}")}
    pm, why = parse_market("KXPAYROLLS", m)
    assert pm is not None, why
    return pm


def test_payroll_strikes_are_scaled_to_thousands():
    assert payroll_market(150000).floor == 150.0
    assert payroll_market(-25000).floor == -25.0


def test_payroll_margin_skips_strikes_within_10k_and_trades_the_rest():
    cfg = releases.settings({})
    near = [payroll_market(150000), payroll_market(160000)]
    far = [payroll_market(125000), payroll_market(175000)]
    assert [margin_ok(pm, 155.0, cfg) for pm in near] == [False, False]
    assert [margin_ok(pm, 155.0, cfg) for pm in far] == [True, True]
    assert margin_ok(payroll_market(145000), 155.0, cfg) is True        # exactly 10k away
    assert margin_ok(payroll_market(145001), 155.0, cfg) is False


def test_non_payroll_series_have_no_strike_margin():
    pm, _ = parse_market("KXU3", fx("KXU3")[1])
    assert margin_ok(pm, 3.8, releases.settings({})) is True


# ---------------------------------------------------------------- rules templates against the captured fixtures
@pytest.mark.parametrize("series", SERIES)
def test_every_fixture_market_parses_with_the_real_rules_text(series):
    ms = fx(series)
    assert ms
    for m in ms:
        pm, why = parse_market(series, m)
        assert pm is not None and why == "", (m["ticker"], why)
        assert pm.series == series
        assert releases.rules_period(series, m["rules_primary"]) is not None


@pytest.mark.parametrize("series", SERIES)
def test_a_modified_rules_text_does_not_match(series):
    for m in fx(series)[:3]:
        for bad in (m["rules_primary"].replace("resolves to Yes", "resolves to No"), m["rules_primary"] + " Extra clause.",
                    "Totally different market."):
            pm, why = parse_market(series, {**m, "rules_primary": bad})
            assert pm is None and why == "rules_mismatch"


@pytest.mark.parametrize("series", ["KXCPI", "KXCPIYOY", "KXU3", "KXFED"])
def test_strike_comes_from_the_fields_and_must_agree_with_the_rules_text(series):
    m = fx(series)[1]
    pm, _ = parse_market(series, m)
    assert pm.strike_type == "greater" and pm.floor == pytest.approx(m["floor_strike"])
    pm2, why = parse_market(series, {**m, "floor_strike": m["floor_strike"] + 1})
    assert pm2 is None and why == "rules_mismatch"                   # field and text disagree: trust neither


def test_missing_strike_type_falls_back_to_the_subtitle_and_skips_when_it_does_not_parse():
    m = {**fx("KXCPI")[7], "strike_type": None, "floor_strike": None}      # T0.3, sub-title "Above 0.3%"
    pm, _ = parse_market("KXCPI", m)
    assert pm.strike_type == "greater" and pm.floor == 0.3
    pm, why = parse_market("KXCPI", {**m, "yes_sub_title": "somewhere around three tenths"})
    assert pm is None and why == "strike_unparsed"
    pm, why = parse_market("KXCPI", {**fx("KXCPI")[7], "strike_type": "weird"})
    assert pm is None and why == "strike_unparsed"


def test_fed_decision_parses_custom_strike_and_cross_checks_the_rules_text():
    by = {m["ticker"].rsplit("-", 1)[1]: m for m in fx("KXFEDDECISION") if "26OCT" in m["ticker"]}
    parsed = {k: parse_market("KXFEDDECISION", m)[0] for k, m in by.items()}
    assert (parsed["C25"].direction, parsed["C25"].bps, parsed["C25"].more) == ("Cut", 25, False)
    assert (parsed["C26"].direction, parsed["C26"].bps, parsed["C26"].more) == ("Cut", 25, True)
    assert (parsed["H0"].direction, parsed["H0"].bps) == ("Hike", 0)
    swapped = {**by["C25"], "custom_strike": {"Hike": "25"}}
    assert parse_market("KXFEDDECISION", swapped) == (None, "rules_mismatch")


def test_resolves_yes_per_strike_type_with_exact_equality():
    mk = lambda t, f=None, c=None: ParsedMarket("T", "KXU3", t, f, c)    # noqa: E731
    assert resolves_yes(mk("greater", 3.8), 3.8) is False and resolves_yes(mk("greater", 3.8), 3.9) is True
    assert resolves_yes(mk("greater_or_equal", 3.8), 3.8) is True
    assert resolves_yes(mk("less", None, 3.8), 3.8) is False and resolves_yes(mk("less_or_equal", None, 3.8), 3.8) is True
    assert resolves_yes(mk("between", 3.0, 4.0), 3.0) is True and resolves_yes(mk("between", 3.0, 4.0), 4.1) is False


def test_kxfed_is_a_ladder_on_the_upper_bound_greater_than():
    mk = lambda f: ParsedMarket("T", "KXFED", "greater", f)               # noqa: E731
    assert [resolves_yes(mk(f), 4.5) for f in (4.25, 4.5, 4.75)] == [True, False, False]


def test_fed_decision_resolution_by_bps_change():
    p = {k: parse_market("KXFEDDECISION", m)[0] for k, m in
         ((m["ticker"].rsplit("-", 1)[1], m) for m in fx("KXFEDDECISION") if "26OCT" in m["ticker"])}
    res = lambda delta: sorted(k for k, pm in p.items() if resolves_yes(pm, delta))   # noqa: E731
    assert res(-25) == ["C25"] and res(0) == ["H0"] and res(25) == ["H25"]
    assert res(-50) == ["C26"] and res(50) == ["H26"]


def test_boundary_with_unproven_inclusivity_is_skipped_but_greater_is_not():
    m = {**fx("KXCPI")[7], "strike_type": "greater_or_equal"}              # same proven text, but an unproven type
    pm, _ = parse_market("KXCPI", m)
    assert on_boundary(pm, 0.3) is True and on_boundary(pm, 0.4) is False
    g, _ = parse_market("KXCPI", fx("KXCPI")[7])
    assert on_boundary(g, 0.3) is False
    cfg = releases.settings({})
    s = ReleaseScheduler(None, None, None, None, cfg=cfg, verbose=False)
    books = {pm.ticker: Book("kalshi", pm.ticker, [(.5, 10)], [(.45, 10)])}
    assert s.rank(CPI, "KXCPI", 0.3, books, [pm]) == []


# ---------------------------------------------------------------- settlement and ranking
def _u3(strikes=(3.7, 3.8, 3.9)):
    return [parse_market("KXU3", m)[0] for m in fx("KXU3") if m["floor_strike"] in strikes and "26OCT" in m["ticker"]]


def test_rank_picks_most_room_drops_priced_in_and_keeps_one_per_series(tmp_ledger):
    s = ReleaseScheduler(tmp_ledger, None, None, None, cfg=releases.settings({}), verbose=False)
    pms = _u3()
    by = {pm.floor: pm.ticker for pm in pms}
    books = {by[3.7]: Book("kalshi", by[3.7], [(.97, 100)], [(.04, 100)]),           # value 3.8 > 3.7: YES, but 97c: dropped
             by[3.8]: Book("kalshi", by[3.8], [(.71, 100)], [(.30, 100)]),           # equal: "greater" is strict, so NO at 30c
             by[3.9]: Book("kalshi", by[3.9], [(.41, 100)], [(.60, 100)])}           # NO at 60c
    picks = s.rank(CPI, "KXU3", 3.8, books, pms)
    assert [(pm.ticker, side, px) for pm, side, _b, px in picks] == [(by[3.8], "no", .30)]
    s.cfg = releases.settings({"RELEASE_MAX_MARKETS_PER_SERIES": "2"})
    two = s.rank(CPI, "KXU3", 3.8, books, pms)
    assert [(pm.ticker) for pm, *_ in two] == [by[3.8], by[3.9]]


def test_rank_drops_markets_without_a_book_or_ask_or_at_95c(tmp_ledger):
    s = ReleaseScheduler(tmp_ledger, None, None, None, cfg=releases.settings({}), verbose=False)
    pms = _u3()
    by = {pm.floor: pm.ticker for pm in pms}
    books = {by[3.7]: Book("kalshi", by[3.7], [(.95, 100)], [(.10, 100)]),       # exactly MAX_ENTRY_PRICE: dropped
             by[3.8]: Book("kalshi", by[3.8], [], [(.30, 100)])}                  # NO ask .30, but value 3.9 -> YES: no yes ask
    assert s.rank(CPI, "KXU3", 3.9, books, pms) == []


def test_unemployment_has_no_rounding_margin_in_rank(tmp_ledger):
    s = ReleaseScheduler(tmp_ledger, None, None, None, cfg=releases.settings({}), verbose=False)
    pms = _u3((4.2,))
    books = {pms[0].ticker: Book("kalshi", pms[0].ticker, [(.40, 10)], [(.55, 10)])}
    assert [side for _pm, side, *_ in s.rank(CPI, "KXU3", 4.3, books, pms)] == ["yes"]   # 4.3 > 4.2, no boundary margin


def test_fresh_books_are_bounded_and_late_books_are_not_candidates(tmp_ledger):
    async def go():
        async def fetch(_h, market):
            if market["id"].endswith("T3.8"):
                await asyncio.sleep(1.0)
            return Book("kalshi", market["id"], [(.5, 10)], [(.45, 10)])
        s = ReleaseScheduler(tmp_ledger, None, None, fetch, cfg=releases.settings({}), quote_wait_s=0.05, verbose=False)
        return await s.fresh_books(_u3())
    got = asyncio.run(go())
    assert sorted(got) == ["KXU3-26OCT-T3.7", "KXU3-26OCT-T3.9"]


# ---------------------------------------------------------------- whole releases against a fake clock
def _cpi_net(clock, sa_new="301.2", nsa_new="300.76"):
    net = Net(clock)

    def serve(req):
        sid = req.url.path.rsplit("/", 1)[1]
        if clock.t < T_CPI:                                                  # not published yet (baseline and early polls)
            return bls_payload([(2026, "M08", "300.0")] if sid == "CUSR0000SA0"
                               else [(2026, "M08", "299.0"), (2025, "M09", "292.0")])
        if sid == "CUSR0000SA0":
            return bls_payload([(2026, "M09", sa_new), (2026, "M08", "300.0")])
        return bls_payload([(2026, "M09", nsa_new), (2026, "M08", "299.0"), (2025, "M09", "292.0")])
    net.bls = serve
    return net


def test_full_cpi_release_trades_one_market_per_series_and_records_the_release(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net, trades = _cpi_net(clock), []
    s = make(tmp_ledger, net, clock, trades=trades)
    status = asyncio.run(s.run_release(CPI))
    assert status == "done"
    assert sorted(t[0]["source"] for t in trades) == ["release:KXCPI", "release:KXCPIYOY"]
    by_series = {t[0]["source"].split(":")[1]: t for t in trades}
    ev, market, side, _bk, value = by_series["KXCPI"]
    assert value == 0.4 and ev["id"] == "release-KXCPI-2026-09" and ev["published_ts"] == T_CPI and ev["synthetic"] is False
    assert "0.4" in ev["headline"] and market["venue"] == "kalshi"
    assert by_series["KXCPIYOY"][4] == 3.0
    row = tmp_ledger.release_get("cpi-2026-09")
    assert row["status"] == "done" and row["value"] == 0.4 and json.loads(row["raw"])["KXCPI"]["settled"] == 0.4
    # 2 baselines, then polls from T-2 until the number appears at T
    n = len(net.of("api.bls.gov"))
    assert 4 <= n <= 12
    assert tmp_ledger.bls_requests(releases.utc_day(clock.t)) == n
    # a restart on the same day never re-arms a release that is done
    assert asyncio.run(make(tmp_ledger, net, clock).run_release(CPI)) == "not_armed"
    assert len(net.of("api.bls.gov")) == n


def test_cpi_within_the_rounding_margin_is_not_traded(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net, trades = _cpi_net(clock, sa_new="301.02"), []                       # +0.34%: 0.01 from the 0.35 boundary
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(CPI))
    assert status == "release_margin" and trades == []
    assert tmp_ledger.release_get("cpi-2026-09")["status"] == "release_margin"


def test_release_never_published_times_out_without_trading(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net, trades = Net(clock), []
    net.bls = lambda req: bls_payload([(2026, "M08", "300.0")])
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(CPI))
    assert status == "timed_out" and trades == []


def test_baseline_that_already_holds_the_period_is_not_traded(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net, trades = Net(clock), []
    net.bls = lambda req: bls_payload([(2026, "M09", "301.0"), (2026, "M08", "300.0")])
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(CPI))
    assert status == "already_published" and trades == []


def test_baseline_failure_is_recorded_and_stops_the_release(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net, trades = Net(clock), []
    net.bls = lambda req: {"status": "REQUEST_NOT_PROCESSED"}
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(CPI))
    assert status == "baseline_failed" and trades == []


# ---------------------------------------------------------------- FOMC
@pytest.mark.parametrize("text,want", [
    ("the Committee decided to maintain the target range for the federal funds rate at 4-1/4 to 4-1/2 percent.", (4.25, 4.5)),
    ("maintain the target range for the federal funds rate at 4 to 4-1/4 percent.", (4.0, 4.25)),
    ("decided to lower the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent.", (3.75, 4.0)),
    ("raise the target range for the federal funds rate by 1/4 percentage point to 4-1/4 to 4-1/2 percent", (4.25, 4.5)),
    ("TARGET RANGE FOR THE FEDERAL FUNDS RATE AT 3-1/2 TO 3-3/4 PERCENT", (3.5, 3.75)),
    ("target range for the federal funds rate at 4.25 to 4.5 percent", (4.25, 4.5)),
    ("target range for the federal funds rate at 0 to 1/4 percent", (0.0, 0.25)),
])
def test_fomc_range_parses_holds_moves_and_fractions(text, want):
    assert fomc_range(text) == want


@pytest.mark.parametrize("text", [
    "no rate language here",
    "target range for the federal funds rate at 4 to 4-1/2 percent",                    # width 0.5
    "target range for the federal funds rate at 4 to 4 percent",                        # width 0
    "target range for the federal funds rate at 4 to 4-1/4 percent and the target range for the federal funds rate at 4 to 4-1/4 percent",
    "target range for the federal funds rate at 10 to 10-1/4 percent",                  # outside [0, 10]
    "", None,
])
def test_fomc_range_doubt_cases_return_none(text):
    assert fomc_range(text) is None


def fed_feed(items):
    its = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate></item>" for t, l, d in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{its}</channel></rss>".encode()


LINK = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm"
STATEMENT_CUT = ("<html><body><p>The Committee decided to lower the target range for the federal funds rate by 1/4 "
                 "percentage point to 3-1/2 to 3-3/4 percent.</p></body></html>")


def test_fomc_cut_release_trades_fed_and_decision_series_from_prior_range(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("Federal Reserve issues FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = STATEMENT_CUT
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC))
    assert status == "done"
    by = {t[0]["source"]: t for t in trades}
    assert set(by) == {"release:KXFED", "release:KXFEDDECISION"}
    assert by["release:KXFED"][4] == 3.75 and by["release:KXFEDDECISION"][4] == -25.0
    assert by["release:KXFEDDECISION"][1]["id"] == "KXFEDDECISION-26OCT-C25" and by["release:KXFEDDECISION"][2] == "yes"
    assert by["release:KXFED"][2] == "yes" and by["release:KXFED"][1]["id"].startswith("KXFED-26OCT-T")
    assert tmp_ledger.release_get("fomc-2026-10")["status"] == "done"
    assert net.of("api.bls.gov") == []                                            # FOMC never touches BLS


def test_fomc_statement_from_yesterday_is_ignored(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("Federal Reserve issues FOMC statement", LINK, "Tue, 27 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = STATEMENT_CUT
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC))
    assert status == "timed_out" and trades == []
    assert not [r for r in net.of("www.federalreserve.gov") if "monetary2026" in r.url.path]    # the page was never fetched


def test_fomc_non_statement_titles_are_ignored(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("Minutes of the Federal Open Market Committee", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = STATEMENT_CUT
    assert asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC)) == "timed_out"


@pytest.mark.parametrize("body", ["<p>Nothing parseable here.</p>",
                                  "<p>target range for the federal funds rate at 4 to 4-1/2 percent</p>"])
def test_fomc_parse_doubt_is_a_no_trade(tmp_ledger, body):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = f"<html><body>{body}</body></html>"
    assert asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC)) == "parse_doubt"
    assert trades == [] and tmp_ledger.release_get("fomc-2026-10")["status"] == "parse_doubt"


def test_fomc_without_prior_range_trades_only_the_range_series(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = STATEMENT_CUT
    entry = CalendarEntry("fomc", "2026-10-28", "14:00", "2026-10")
    asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(entry))
    assert [t[0]["source"] for t in trades] == ["release:KXFED"]


def test_fomc_hold_maps_to_the_maintain_market(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = "<p>maintain the target range for the federal funds rate at 3-3/4 to 4 percent.</p>"
    asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(FOMC))
    dec = [t for t in trades if t[0]["source"] == "release:KXFEDDECISION"][0]
    assert dec[4] == 0.0 and dec[1]["id"] == "KXFEDDECISION-26OCT-H0"


# ---------------------------------------------------------------- --check
def test_check_with_empty_calendar_and_no_key_sends_nothing(tmp_path, tmp_ledger):
    out, net = [], Net()
    cfg = releases.settings({})

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
        n = await releases.check("2026-10-14", cfg, calendar_path=write_cal(tmp_path, []), ledger=tmp_ledger, http=http,
                                 out=out.append)
        await http.aclose()
        return n
    assert asyncio.run(go()) == 0 and net.reqs == []
    assert any("BLS_API_KEY: not set" in l for l in out) and any("nothing to check" in l for l in out)


def test_check_prints_the_parse_table_with_get_only_and_no_bls_without_a_key(tmp_path, tmp_ledger):
    out, net = [], Net()
    cfg = releases.settings({})
    cal = write_cal(tmp_path, [{"kind": "cpi", "date": "2026-10-14", "time_et": "08:30", "period": "2026-09"}])

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
        n = await releases.check("2026-10-14", cfg, calendar_path=cal, ledger=tmp_ledger, http=http, out=out.append)
        await http.aclose()
        return n
    n = asyncio.run(go())
    assert n == len(net.reqs) == 2 and {r.method for _, r in net.reqs} == {"GET"} and net.of("api.bls.gov") == []
    text = "\n".join(out)
    assert "KXCPI: 14 markets with a matching rules template" in text and "no BLS key: baseline skipped" in text


def test_main_check_with_no_entry_for_the_date_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("fastlane.config.load_env", lambda: None)
    assert releases.main(["--check", "--date", "2000-01-01"]) == 0
    assert "nothing to check" in capsys.readouterr().out


def test_main_without_check_prints_help_and_does_not_run(capsys):
    assert releases.main([]) == 0 and "--check" in capsys.readouterr().out


# ---------------------------------------------------------------- engine: release trades
MKT = {"venue": "kalshi", "id": "KXCPI-26SEP-T0.3", "question": "CPI above 0.3%", "category": "Economics"}


def _release_ev(published=None, seen=None, series="KXCPI"):
    import time as _t
    now = _t.time()
    return {"id": f"release-{series}-2026-09", "source": f"release:{series}", "headline": f"CPI 2026-09: x = 0.4",
            "summary": "{}", "url": "u", "published_ts": published or now - 1, "seen_ts": seen or now, "synthetic": False}


GOOD_BOOK = Book("kalshi", MKT["id"], [(.55, 1000)], [(.47, 1000)])


def _trade_release(e, ev, side="yes", bk=GOOD_BOOK):
    async def go():
        try:
            return await e.trade_release(ev, MKT, side, bk, 0.4)
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    return asyncio.run(go())


def _drow(e, event_id):
    cur = e.ledger.db.execute("SELECT action, reason, jev_ms, market_conf, materiality, shadow_action, starter_action, "
                              "n_candidates FROM decisions WHERE event_id = ?", (event_id,))
    return cur.fetchone()


def test_release_trade_is_a_live_book_trade_with_no_jev_and_no_side_books(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    ev = _release_ev()
    rec = _trade_release(e, ev)
    assert (rec["action"], rec["reason"]) == ("BUY_YES", "release_yes")
    assert _drow(e, ev["id"]) == ("BUY_YES", "release_yes", None, 1.0, 1.0, None, None, 1)
    assert e.ledger.db.execute("SELECT source FROM events WHERE id=?", (ev["id"],)).fetchone() == ("release:KXCPI",)
    t = e.ledger.db.execute("SELECT book, shadow, synthetic, market_id, entry_style, signal_strength FROM trades").fetchall()
    assert t == [("live", 0, 0, MKT["id"], "take", 1.0)]
    assert e.trades == 1 and e.jev.calls == 0 and e.ledger.orders_working() == []


def test_release_no_signal_reason(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    ev = _release_ev()
    rec = _trade_release(e, ev, side="no")
    assert (rec["action"], rec["reason"]) == ("BUY_NO", "release_no")


def test_release_stale_news_eleven_minutes_late_is_blocked(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    import time as _t
    now = _t.time()
    ev = _release_ev(published=now - 700, seen=now - 40)           # fetched 11 min 20 s after T
    rec = _trade_release(e, ev)
    assert rec["reason"] == "stale_news" and e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_release_priced_in_when_the_tape_moved_three_cents_our_way(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.tape_enabled = True
    import time as _t
    now = _t.time()
    e.tape.hist[MKT["id"]].append((now - 30, .49, .51))            # mid .50 at the release; the book now mids .54
    ev = _release_ev(published=now - 2, seen=now)
    rec = _trade_release(e, ev)
    assert rec["reason"] == "priced_in" and e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_release_cost_block_is_a_pass(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    wide = Book("kalshi", MKT["id"], [(.55, 1000)], [(.50, 1000)])   # 5c spread
    ev = _release_ev()
    rec = _trade_release(e, ev, bk=wide)
    assert (rec["action"], rec["reason"]) == ("PASS", "too_expensive")
    assert e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_release_already_in_market_is_blocked(monkeypatch, tmp_path, tiny_universe):
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    _trade_release(e, _release_ev())
    e.http = httpx.AsyncClient()
    rec = _trade_release(e, {**_release_ev(), "id": "release-KXCPI-2026-09-m1"})
    assert rec["reason"] == "already_in_market"


def test_release_on_paper_sends_no_request(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)           # configured but not armed
    _trade_release(e, _release_ev())
    assert rec_.requests == [] and e.ledger.db.execute("SELECT COUNT(*) FROM trades WHERE book='live'").fetchone()[0] == 1


def test_release_yes_armed_sends_exactly_one_ioc_order_at_the_paper_limit(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    live.arm(e.live.session)
    _trade_release(e, _release_ev())
    assert len(rec_.requests) == 1
    body = json.loads(rec_.requests[0].content)
    assert body["ticker"] == MKT["id"] and body["price"] == "0.5800" and body["time_in_force"] == "immediate_or_cancel"


def test_release_no_signal_armed_stays_paper_with_the_no_side_message(monkeypatch, tmp_path, tiny_universe, rsa_pem, capsys):
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    live.arm(e.live.session)
    _trade_release(e, _release_ev(), side="no")
    assert rec_.requests == []
    assert "real NO orders disabled until verified" in capsys.readouterr().out
    assert e.ledger.db.execute("SELECT COUNT(*) FROM trades WHERE book='live'").fetchone()[0] == 1


def test_release_respects_the_hourly_cap(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    monkeypatch.setenv("LIVE_MAX_ORDERS_PER_HOUR", "0")
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    live.arm(e.live.session)
    _trade_release(e, _release_ev())
    assert rec_.requests == []


def test_release_without_the_env_switch_never_orders_even_with_a_stale_arm(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_, enabled=False)
    live.arm(e.live.session)
    _trade_release(e, _release_ev())
    assert rec_.requests == []


def test_release_post_style_rests_for_paper_when_not_armed(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    rec = _trade_release(e, _release_ev())
    assert rec["reason"] == "post_working" and len(e.ledger.orders_working()) == 1


# ---------------------------------------------------------------- review fixes: FOMC guards, etag, poll cadence
def _fomc_run(tmp_ledger, body, entry=FOMC):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    net.pages[LINK] = f"<html><body><p>{body}</p></body></html>"
    status = asyncio.run(make(tmp_ledger, net, clock, trades=trades).run_release(entry))
    return status, trades


def test_fomc_rate_change_outside_the_allowed_set_is_no_trade(tmp_ledger):
    # prior 3.75-4: a statement at 3 to 3-1/4 is a 75 bp cut, so the owner data or the parse is wrong
    status, trades = _fomc_run(tmp_ledger, "lower the target range for the federal funds rate by 3/4 percentage point to 3 to 3-1/4 percent.")
    assert status == "parse_doubt" and trades == []
    assert tmp_ledger.release_get("fomc-2026-10")["status"] == "parse_doubt"


def test_fomc_prior_range_that_disagrees_with_the_statements_from_range_is_no_trade(tmp_ledger):
    status, trades = _fomc_run(tmp_ledger, "lower the target range for the federal funds rate by 1/4 percentage point to 3-1/2 to 3-3/4 "
                                           "percent, from 4-1/4 to 4-1/2 percent.")
    assert status == "parse_doubt" and trades == []


def test_fomc_matching_from_range_still_trades(tmp_ledger):
    status, trades = _fomc_run(tmp_ledger, "lower the target range for the federal funds rate by 1/4 percentage point to 3-1/2 to 3-3/4 "
                                           "percent, from 3-3/4 to 4 percent.")
    assert status == "done" and {t[0]["source"] for t in trades} == {"release:KXFED", "release:KXFEDDECISION"}


def test_fomc_failed_statement_page_is_retried_after_a_304_prone_feed(tmp_ledger):
    clock = Clock(ts("2026-10-28", "13:50"))
    net, trades = Net(clock), []
    net.feed_xml = fed_feed([("FOMC statement", LINK, "Wed, 28 Oct 2026 18:00:00 GMT")])
    inner = net.handler
    page_gets = []

    def handler(req):
        if req.url.path.endswith("press_monetary.xml") and req.headers.get("if-none-match") == "x":
            return httpx.Response(304)                       # the real feed answers 304 once it has seen our ETag
        if str(req.url) == LINK:
            page_gets.append(clock.t)
            if len(page_gets) == 1:
                return httpx.Response(500)                   # first page GET fails
            return httpx.Response(200, text=STATEMENT_CUT)
        return inner(req)
    net.handler = handler
    s = make(tmp_ledger, net, clock, trades=trades)
    s.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(s.run_release(FOMC)) == "done" and len(page_gets) == 2 and trades


def test_a_slow_bls_answer_never_causes_back_to_back_polls(tmp_ledger):
    clock = Clock(T_CPI - 300)
    net = Net(clock)
    calls = []

    def serve(req):
        calls.append(clock.t)
        if req.url.params.get("startyear") and len(calls) in (3, 4, 5, 6):
            clock.t += 10                                      # four slow answers
        clock.t += 0.01
        return bls_payload([(2026, "M08", "300.0")])
    net.bls = serve
    asyncio.run(make(tmp_ledger, net, clock).run_release(CPI))
    rounds = sorted({round(t, 1) for t in calls[6:]})
    gaps = [b - a for a, b in zip(rounds, rounds[1:])]
    assert gaps and min(gaps) >= 1.4
