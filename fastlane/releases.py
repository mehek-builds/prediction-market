"""Scheduled data releases: read the number from the official source in the first seconds, buy the Kalshi strikes (and Polymarket Fed brackets) it settles.

No LLM, no news feed: for a few scheduled releases (CPI, jobs report, FOMC statement) the settlement value is a number
published by a known source at a known second, and the Kalshi ladders on that number are plain threshold markets.
Everything here is GET only (BLS v2 API, the Federal Reserve statement feed, Kalshi public market data, Polymarket gamma and CLOB). Trades go
through Engine.trade_release into the LIVE paper book and from there through the same real-money locks as any other
trade; this module cannot send an order.

Safety rules, in short:
  * the owner-maintained fastlane/release_calendar.json is the only schedule; an empty calendar never arms
  * a market is traded only if its rules text matches the template captured from the live Kalshi rules (RULES_TEMPLATES)
    AND names the release month (or meeting date) in the calendar entry AND its strike field agrees with the rules text
  * BLS is polled only with a registered key (v2), counted in the ledger BEFORE each request, inside a daily budget
  * CPI values within RELEASE_MARGIN_CPI_PP of a rounding boundary are not traded, payrolls within the margin of a strike
    are not traded, an FOMC statement that does not parse to exactly one 25 bp wide range is not traded

    python3 -m fastlane.releases --check [--date YYYY-MM-DD]
"""
import argparse
import asyncio
import heapq
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import httpx

from fastlane import errors, poly_fomc
from fastlane.books import MAX_ENTRY_PRICE, Book, fetch_book
from fastlane.config import PKG, ROOT, kalshi_api_url
from fastlane.feeds import _clean
from fastlane.ledger import utc_day

CALENDAR_PATH = PKG / "release_calendar.json"
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/{series}"   # v2 only; the keyless v1 API is never used
FED_FEED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
USER_AGENT = "fastlane-releases/0.5 (read-only; github.com/fastlane)"
ET = "America/New_York"
KINDS = ("cpi", "jobs", "fomc")
MAX_MARKET_PAGES = 5

BASELINE_LEAD_S = 60        # baseline fetch this long before the release
MARKETS_LEAD_S = 120        # Kalshi markets and rules texts this long before
BOOK_PREFETCH_LEAD_S = 5    # warm the order books this long before
FOMC_LEAD_S = 5             # start polling the Fed this long before the release
FOMC_FAST_POLL_S = 0.5      # one round (statement URL + feed) every 0.5 s for the first minute
FOMC_FAST_WINDOW_S = 60.0
FOMC_POLL_S = 2.0           # then every 2 s
FOMC_MAX_S = 300.0
FOMC_MAX_REQUESTS = 500     # hard cap on GETs to the Fed per release: 2 x (120 + 120) = 480 at the cadence above
FED_STATEMENT_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
FED_HOST = "www.federalreserve.gov"
RELEASE_BOOK_HORIZONS_S = (5, 30, 60)
MAX_BEHIND_S = 300          # an entry whose window ended this long ago is not started late
IDLE_S = 60.0


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class ReleaseSettings:
    enabled: bool
    bls_key: str = field(repr=False)
    tz: ZoneInfo
    poll_start_s: float
    poll_every_s: float
    poll_max_s: float
    daily_budget: int
    margin_cpi_pp: float
    margin_payrolls_k: float
    max_markets_per_series: int
    poly_fomc: bool = True         # POLY_FOMC_ENABLED: trade the Polymarket "Fed Decision in <Month>?" brackets (paper only)
    poly_max_no: int = 1           # POLY_FOMC_MAX_NO: dead brackets bought NO after the YES (0 = none)


def settings(env=None) -> ReleaseSettings:
    """Release settings from env (RELEASES_ENABLED, BLS_API_KEY, RELEASE_*). A malformed number raises ValueError."""
    e = env if env is not None else os.environ

    def num(name: str, default: float, minimum: float | None = None) -> float:
        raw = (e.get(name) or "").strip()
        if not raw:
            v = default
        else:
            try:
                v = float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number (got {raw!r})") from exc
        if not math.isfinite(v):
            raise ValueError(f"{name} must be a finite number")
        return max(v, minimum) if minimum is not None else v

    enabled = (e.get("RELEASES_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")
    return ReleaseSettings(
        enabled=enabled, bls_key=(e.get("BLS_API_KEY") or "").strip(), tz=ZoneInfo(ET),
        poll_start_s=max(num("RELEASE_POLL_START_S", 2.0), 0.0), poll_every_s=num("RELEASE_POLL_EVERY_S", 1.5, 1.0),
        poll_max_s=num("RELEASE_POLL_MAX_S", 90.0, 1.0), daily_budget=int(num("RELEASE_BLS_DAILY_BUDGET", 400, 0)),
        margin_cpi_pp=num("RELEASE_MARGIN_CPI_PP", 0.02, 0.0), margin_payrolls_k=num("RELEASE_MARGIN_PAYROLLS_K", 10, 0.0),
        max_markets_per_series=int(num("RELEASE_MAX_MARKETS_PER_SERIES", 1, 1)),
        poly_fomc=(e.get("POLY_FOMC_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on"),
        poly_max_no=int(num("POLY_FOMC_MAX_NO", 1, 0)))


# ---------------------------------------------------------------- series
@dataclass(frozen=True)
class SeriesSpec:
    series: str                  # Kalshi series ticker
    kind: str                    # calendar kind
    statistic: str               # what the market settles on
    bls: tuple[str, ...] = ()    # BLS series id needed
    decimals: int | None = None  # rounding the market settles at (None: not a rounded number)
    scale: float = 1.0           # Kalshi strike unit per unit of our value (payrolls: persons per thousand)
    venue: str = "kalshi"


SERIES: dict[str, SeriesSpec] = {
    "KXCPI": SeriesSpec("KXCPI", "cpi", "CPI-U month-over-month % change, seasonally adjusted", ("CUSR0000SA0",), 1),
    "KXCPIYOY": SeriesSpec("KXCPIYOY", "cpi", "CPI-U year-over-year % change, not seasonally adjusted", ("CUUR0000SA0",), 1),
    "KXPAYROLLS": SeriesSpec("KXPAYROLLS", "jobs", "change in total nonfarm payrolls, thousands", ("CES0000000001",), 0, 1000.0),
    "KXU3": SeriesSpec("KXU3", "jobs", "unemployment rate, seasonally adjusted", ("LNS14000000",), 1),
    "KXFED": SeriesSpec("KXFED", "fomc", "upper bound of the federal funds target range"),
    "KXFEDDECISION": SeriesSpec("KXFEDDECISION", "fomc", "change of the target range vs the prior range, bps"),
    "POLYFED": SeriesSpec("POLYFED", "fomc", "Fed decision bracket, Polymarket (bps change of the upper bound)",
                          venue="polymarket"),
}


def series_for(kind: str) -> list[str]:
    return [s for s, sp in SERIES.items() if sp.kind == kind]


def bls_series_for(kind: str) -> list[str]:
    out: list[str] = []
    for s in series_for(kind):
        out += [b for b in SERIES[s].bls if b not in out]
    return out


def planned_requests(kind: str, st: ReleaseSettings) -> int:
    """BLS requests one release can use: 1 baseline per series + one per poll round per series."""
    n = len(bls_series_for(kind))
    return n * (1 + math.ceil(st.poll_max_s / st.poll_every_s)) if n else 0


# ---------------------------------------------------------------- rounding
def settled(value: float, decimals: int) -> float:
    """The value Kalshi settles on: rounded half away from zero (the BLS convention) to `decimals` places."""
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def rounds_safely(value: float, decimals: int, margin: float) -> bool:
    """True when `value` is at least `margin` away from the nearest rounding boundary at `decimals` places
    (the boundaries sit halfway between two representable values: x.x5 for one decimal)."""
    step = Decimal(1).scaleb(-decimals)
    frac = (Decimal(str(abs(value))) / step) % 1   # abs: Decimal % keeps the dividend's sign, and rounding is symmetric about 0
    return abs(frac - Decimal("0.5")) * step >= Decimal(str(margin))


# ---------------------------------------------------------------- calendar
@dataclass(frozen=True)
class CalendarEntry:
    kind: str
    date: str                                   # YYYY-MM-DD (ET)
    time_et: str                                # HH:MM
    period: str                                 # YYYY-MM (reference month; fomc: the meeting month)
    prior_range: tuple[float, float] | None = None   # fomc only: target range before the meeting, percent

    @property
    def release_id(self) -> str:
        return f"{self.kind}-{self.period}"

    def scheduled_ts(self, tz: ZoneInfo) -> float:
        hh, mm = (int(x) for x in self.time_et.split(":"))
        y, mo, d = (int(x) for x in self.date.split("-"))
        return datetime(y, mo, d, hh, mm, tzinfo=tz).timestamp()

    @property
    def year_month(self) -> tuple[int, int]:
        y, m = self.period.split("-")
        return int(y), int(m)


def _entry(i: int, raw) -> CalendarEntry:
    def bad(why: str):
        return ValueError(f"release_calendar.json events[{i}]: {why}")

    if not isinstance(raw, dict):
        raise bad("must be an object")
    extra = set(raw) - {"kind", "date", "time_et", "period", "prior_range"}
    if extra:
        raise bad(f"unknown keys {sorted(extra)}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise bad(f"kind must be one of {', '.join(KINDS)}")
    d, t, p = raw.get("date"), raw.get("time_et"), raw.get("period")
    try:
        if not (isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)):
            raise ValueError
        date.fromisoformat(d)
    except ValueError:
        raise bad("date must be a real YYYY-MM-DD") from None
    if not (isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)):
        raise bad("time_et must be HH:MM")
    if not (isinstance(p, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", p)):
        raise bad("period must be YYYY-MM")
    if kind == "fomc" and d[:7] != p:   # every FOMC series keys its markets on the period, so it must be the meeting's month
        raise bad(f"fomc period {p} must be the month of date {d}")
    pr = raw.get("prior_range")
    prior = None
    if pr is not None:
        if kind != "fomc":
            raise bad("prior_range is for fomc entries only")
        if (not isinstance(pr, (list, tuple)) or len(pr) != 2
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in pr)):
            raise bad("prior_range must be [lo, hi] in percent")
        lo, hi = float(pr[0]), float(pr[1])
        if not (0 <= lo < hi <= 10):
            raise bad("prior_range must satisfy 0 <= lo < hi <= 10")
        prior = (lo, hi)
    return CalendarEntry(kind, d, t, p, prior)


def load_calendar(path: Path = CALENDAR_PATH) -> list[CalendarEntry]:
    """Validate every entry of the owner's calendar file. ValueError names the bad entry's index."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError as exc:
        raise ValueError(f"release calendar unreadable: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"release calendar is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError('release_calendar.json must be an object with an "events" list')
    return [_entry(i, raw) for i, raw in enumerate(data["events"])]


def today_entries(cal: list[CalendarEntry], now: float, tz: ZoneInfo) -> list[CalendarEntry]:
    day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    return [e for e in cal if e.date == day]


# ---------------------------------------------------------------- Kalshi rules and strikes
_NUM = r"-?\d+(?:\.\d+)?"
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_MON3 = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
# One regex per series, written from the live rules_primary texts saved in tests/fixtures/release_rules/. A rules text
# that does not match (a changed definition, a different source, a different rounding sentence) is never traded.
# Caveat recorded for the owner: the KXCPI text says "increases by more than X% ... in <month>" and the single-decimal
# sentence, but does not itself say seasonally adjusted or month-over-month; KXPAYROLLS does not say seasonally
# adjusted either. The statistic mapping in SERIES is Kalshi's published definition, not something the text proves.
RULES_TEMPLATES: dict[str, re.Pattern] = {
    "KXCPI": re.compile(
        rf"^If the Consumer Price Index \(CPI\) increases by more than (?P<strike>{_NUM})% "
        rf"(?:\(single-decimal\) in (?P<m1>{_MONTH}) (?P<y1>\d{{4}})|in (?P<m2>{_MONTH}) (?P<y2>\d{{4}}), "
        rf"as reported to one decimal place), then the market resolves to Yes\.$"),
    "KXCPIYOY": re.compile(
        rf"^If the Consumer Price Index \(CPI\) increases by more than (?P<strike>{_NUM})% in the twelve months ending "
        rf"(?P<m1>{_MONTH}) (?P<y1>\d{{4}}) \(as represented by the one-decimal place value reported by the Bureau of "
        rf"Labor Statistics\), then the market resolves to Yes\.$"),
    "KXPAYROLLS": re.compile(
        rf"^If the increase in total non-farm payroll employment is above (?P<strike>-?\d+) as reported by the Bureau of "
        rf"Labor Statistics Monthly Employment Situation Report for the month of (?P<m1>{_MONTH}) (?P<y1>\d{{4}}), "
        rf"then the market resolves to Yes\.$"),
    "KXU3": re.compile(
        rf"^If the seasonally adjusted unemployment rate \(U-3\) reported by the Bureau of Labor Statistics in the "
        rf"Employment Situation Report is above (?P<strike>{_NUM})% in (?P<m1>{_MONTH}) (?P<y1>\d{{4}}), then the "
        rf"market resolves to Yes\.$"),
    "KXFED": re.compile(
        rf"^If the upper bound of the target federal funds rate published on the Federal Reserve's official website is "
        rf"greater than (?P<strike>{_NUM})% following the Federal Reserve's (?P<m1>{_MON3}) (?P<d1>\d{{1,2}}), "
        rf"(?P<y1>\d{{4}}) meeting, then the market resolves to Yes\.$"),
    "KXFEDDECISION": re.compile(
        rf"^If the Federal Reserve does a (?P<dir>Cut|Hike) of\s*>?\s*(?P<bps>\d+)bps on (?P<m1>{_MONTH}) (?P<d1>\d{{1,2}}), "
        rf"(?P<y1>\d{{4}}), then the market resolves to Yes\.$"),
}
# "greater" is the only strike type seen on these series. Its rules text says "more than" / "above" / "greater than":
# strictly greater, so a value equal to the strike resolves NO. Any other type is not proven inclusive or exclusive by a
# captured text, so a value equal to its bound is skipped (strike_boundary).
INCLUSIVITY_PROVEN = frozenset({"greater"})
STRIKE_TYPES = frozenset({"greater", "greater_or_equal", "less", "less_or_equal", "between"})
# Fallbacks when a market carries no strike_type: one regex per series on yes_sub_title.
SUBTITLE_RES: dict[str, re.Pattern] = {
    s: re.compile(rf"^Above (?P<strike>-?[\d,]+(?:\.\d+)?)%?$")
    for s in ("KXCPI", "KXCPIYOY", "KXPAYROLLS", "KXU3", "KXFED")}
SUBTITLE_RES["KXFEDDECISION"] = re.compile(r"^(?:(?P<dir>Cut|Hike) (?P<more>>?)(?P<bps>\d+) ?bps|(?P<hold>Fed maintains rate))$")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


@dataclass(frozen=True)
class ParsedMarket:
    ticker: str
    series: str
    strike_type: str                 # greater | greater_or_equal | less | less_or_equal | between | custom
    floor: float | None = None       # in our units (payrolls: thousands)
    cap: float | None = None
    direction: str | None = None     # custom (KXFEDDECISION): Cut | Hike
    bps: int | None = None
    more: bool = False               # custom: ">25"
    question: str = ""
    close_time: str = ""
    venue: str = "kalshi"
    bracket: str | None = None       # polymarket FOMC: cut_50 | cut_25 | hold | hike_25 | hike_50
    yes_token: str = ""
    no_token: str = ""

    def as_json(self) -> str:
        return json.dumps({k: v for k, v in self.__dict__.items()})


def _num(x) -> float | None:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_market(series: str, m: dict) -> tuple[ParsedMarket | None, str]:
    """(ParsedMarket, "") or (None, reason). Strike comes from the market's own fields; only a market without a strike type
    falls back to the sub-title regex. The rules text must match the series template and agree with the strike field."""
    spec = SERIES[series]
    rules = m.get("rules_primary") or ""
    tmpl = RULES_TEMPLATES.get(series)
    if tmpl is None:
        return None, "rules_mismatch"            # no captured template: the series is skipped
    rm = tmpl.match(rules)
    if not rm:
        return None, "rules_mismatch"
    ticker = m.get("ticker") or ""
    q = f"{m.get('title') or ''} {m.get('yes_sub_title') or ''}".strip()
    st = m.get("strike_type")
    if series == "KXFEDDECISION":
        cs = m.get("custom_strike")
        direction = more = bps = None
        if isinstance(cs, dict) and len(cs) == 1:
            (direction, raw), = cs.items()
            more = str(raw).startswith(">")
            bps = _num(str(raw).lstrip(">"))
        else:
            sm = SUBTITLE_RES[series].match(m.get("yes_sub_title") or "")
            if sm:
                if sm.group("hold"):
                    direction, bps, more = "Hike", 0.0, False
                else:
                    direction, bps, more = sm.group("dir"), _num(sm.group("bps")), bool(sm.group("more"))
        if direction not in ("Cut", "Hike") or bps is None:
            return None, "strike_unparsed"
        if direction != rm.group("dir") or int(bps) != int(rm.group("bps")):
            return None, "rules_mismatch"        # the field and the rules text must say the same thing
        return ParsedMarket(ticker, series, "custom", direction=direction, bps=int(bps), more=bool(more), question=q,
                            close_time=m.get("close_time") or ""), ""
    floor, cap = _num(m.get("floor_strike")), _num(m.get("cap_strike"))
    if st in STRIKE_TYPES and (floor is not None or cap is not None):
        pass
    elif st is None or st == "":
        sm = SUBTITLE_RES[series].match(m.get("yes_sub_title") or "")
        v = _num(sm.group("strike")) if sm else None
        if v is None:
            return None, "strike_unparsed"
        st, floor, cap = "greater", v, None
    else:
        return None, "strike_unparsed"
    need = {"greater": floor, "greater_or_equal": floor, "less": cap, "less_or_equal": cap}
    if st == "between" and (floor is None or cap is None):
        return None, "strike_unparsed"
    if st in need and need[st] is None:
        return None, "strike_unparsed"
    rule_strike = _num(rm.group("strike"))
    if st == "greater" and (rule_strike is None or abs(rule_strike - floor) > 1e-9):
        return None, "rules_mismatch"            # the rules text and the strike field disagree: trust neither
    if spec.scale != 1.0:
        floor = None if floor is None else floor / spec.scale
        cap = None if cap is None else cap / spec.scale
    return ParsedMarket(ticker, series, st, floor, cap, question=q, close_time=m.get("close_time") or ""), ""


def rules_period(series: str, rules: str) -> tuple[int, int, int | None] | None:
    """(year, month, day-or-None) named by the rules text, or None when the text does not match the template."""
    rm = RULES_TEMPLATES[series].match(rules or "") if series in RULES_TEMPLATES else None
    if not rm:
        return None
    g = rm.groupdict()
    month = g.get("m1") or g.get("m2")
    year = g.get("y1") or g.get("y2")
    mi = next((i for i, n in enumerate(_MONTHS) if n[:3] == month[:3]), None)
    if mi is None:
        return None
    return int(year), mi + 1, int(g["d1"]) if g.get("d1") else None


def market_in_release(series: str, rules: str, entry: CalendarEntry) -> bool:
    """The rules text names this release: the reference month (cpi, jobs) or the meeting date (fomc)."""
    rp = rules_period(series, rules)
    if rp is None:
        return False
    y, mo, d = rp
    if entry.kind == "fomc":
        ey, em, ed = (int(x) for x in entry.date.split("-"))
        return (y, mo, d) == (ey, em, ed)
    return (y, mo) == entry.year_month


def resolves_yes(pm: ParsedMarket, v: float) -> bool:
    """Settlement of one market for the settled value `v`, from the strike fields (exclusive for "greater", see
    INCLUSIVITY_PROVEN). KXFEDDECISION markets take the change in bps."""
    if pm.strike_type == "bracket":
        return pm.bracket == poly_fomc.BRACKET_OF_BPS.get(int(v))
    if pm.strike_type == "custom":
        target = -pm.bps if pm.direction == "Cut" else pm.bps
        if pm.more:
            return v < target if pm.direction == "Cut" else v > target
        return v == target
    if pm.strike_type == "greater":
        return v > pm.floor
    if pm.strike_type == "greater_or_equal":
        return v >= pm.floor
    if pm.strike_type == "less":
        return v < pm.cap
    if pm.strike_type == "less_or_equal":
        return v <= pm.cap
    return pm.floor <= v <= pm.cap


def on_boundary(pm: ParsedMarket, v: float) -> bool:
    """True when the value sits on a bound of a market whose inclusivity no captured rules text proves."""
    if pm.strike_type in INCLUSIVITY_PROVEN or pm.strike_type in ("custom", "bracket"):
        return False
    return any(b is not None and abs(b - v) < 1e-9 for b in (pm.floor, pm.cap))


def margin_ok(pm: ParsedMarket, v: float, st: ReleaseSettings) -> bool:
    """Payrolls keep RELEASE_MARGIN_PAYROLLS_K (thousands) from every strike bound; other series have no strike margin
    (CPI is checked on the value's rounding boundary, unemployment is published already rounded)."""
    if pm.series != "KXPAYROLLS":
        return True
    return all(b is None or abs(v - b) >= st.margin_payrolls_k - 1e-9 for b in (pm.floor, pm.cap))


# ---------------------------------------------------------------- BLS data
@dataclass(frozen=True)
class Point:
    year: int
    month: int
    value: float
    preliminary: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return self.year, self.month


def parse_bls(payload) -> list[Point]:
    """Monthly points, newest first. Anything that is not a clean REQUEST_SUCCEEDED answer gives []: the caller keeps
    polling. M13 (the annual average) is ignored."""
    try:
        if payload.get("status") != "REQUEST_SUCCEEDED":
            return []
        rows = payload["Results"]["series"][0]["data"]
    except (AttributeError, KeyError, IndexError, TypeError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            per = r["period"]
            if not re.fullmatch(r"M(0[1-9]|1[0-2])", per):
                continue
            prelim = any(f.get("code") == "P" for f in (r.get("footnotes") or []) if isinstance(f, dict))
            out.append(Point(int(r["year"]), int(per[1:]), float(str(r["value"]).replace(",", "")), prelim))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda p: p.key, reverse=True)


def _prev(y: int, m: int, months: int = 1) -> tuple[int, int]:
    n = y * 12 + (m - 1) - months
    return n // 12, n % 12 + 1


def compute_stat(series: str, period: tuple[int, int], baseline: dict[str, list[Point]], new: dict[str, list[Point]]):
    """(raw, settled) for one Kalshi series, or None when the inputs are incomplete.

    CPI MoM: (idx_t / idx_{t-1} - 1) * 100 on the seasonally adjusted index; YoY: against t-12 on the not adjusted index;
    the prior index comes from the same new response (revised history). Payrolls: level_t - level_{t-1} with both levels
    from the SAME new response, so the prior month is the revised one, as in the headline. Unemployment: as published."""
    spec = SERIES[series]
    y, m = period
    bid = spec.bls[0]
    cur = {p.key: p.value for p in new.get(bid, [])}
    if (y, m) not in cur:
        return None
    if series == "KXCPI" or series == "KXCPIYOY":
        # both indexes from the SAME new response: the January release revises the seasonally adjusted history, so a
        # prior month taken from the pre-release baseline would be stale. The baseline only says "is this a new period".
        ref = _prev(y, m, 1 if series == "KXCPI" else 12)
        if ref not in cur or cur[ref] == 0:
            return None
        raw = (cur[(y, m)] / cur[ref] - 1) * 100
        return raw, settled(raw, 1)
    if series == "KXPAYROLLS":
        ref = _prev(y, m)
        if ref not in cur:
            return None
        raw = cur[(y, m)] - cur[ref]
        return raw, float(round(raw))
    if series == "KXU3":
        raw = cur[(y, m)]
        return raw, settled(raw, 1)
    return None


# ---------------------------------------------------------------- FOMC
_FRAC = r"\d+(?:-\d+/\d+)?|\d+/\d+|\d+(?:\.\d+)?"
FOMC_RE = re.compile(
    rf"target range for the federal funds rate (?:by (?:{_FRAC}) percentage points? )?(?:at|to) ({_FRAC}) to ({_FRAC}) percent",
    re.I)


def _rate(txt: str) -> float:
    if "-" in txt:                      # "3-3/4"
        whole, frac = txt.split("-", 1)
        n, d = frac.split("/")
        return int(whole) + int(n) / int(d)
    if "/" in txt:                      # "1/4"
        n, d = txt.split("/")
        return int(n) / int(d)
    return float(txt)


def fomc_range(text: str) -> tuple[float, float] | None:
    """The new target range from statement text: exactly ONE match, 0.25 wide, inside [0, 10]; else None (parse_doubt).
    The statement words are "... the target range for the federal funds rate at 3-3/4 to 4 percent" for a hold and
    "... by 1/4 percentage point to 3-3/4 to 4 percent" for a move."""
    found = FOMC_RE.findall(text or "")
    if len(found) != 1:
        return None
    try:
        lo, hi = _rate(found[0][0]), _rate(found[0][1])
    except (ValueError, ZeroDivisionError):
        return None
    if not (0 <= lo and hi <= 10) or abs((hi - lo) - 0.25) > 1e-9:
        return None
    return lo, hi


FOMC_MOVES = (-0.50, -0.25, 0.0, 0.25, 0.50)     # a rate change (pp) outside this set means we misread something
FROM_RE = re.compile(rf"from ({_FRAC}) to ({_FRAC}) percent", re.I)


def fomc_from_range(text: str) -> tuple[float, float] | None:
    """The statement's own "from X to Y percent" range when it states exactly one, else None."""
    found = FROM_RE.findall(text or "")
    if len(found) != 1:
        return None
    try:
        return _rate(found[0][0]), _rate(found[0][1])
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------- scheduler
class BudgetHit(Exception):
    """The BLS daily request budget is used up."""


def close_ts(close_time: str) -> float | None:
    """Epoch seconds of an ISO close_time ("...Z"), or None when missing or unparseable."""
    try:
        return datetime.fromisoformat((close_time or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class ArmedRelease:
    entry: CalendarEntry
    scheduled_ts: float
    markets: dict[str, list[ParsedMarket]]
    planned: int


class ReleaseScheduler:
    def __init__(self, ledger, http, trade, fetch_book, cfg: ReleaseSettings | None = None,
                 calendar_path: Path = CALENDAR_PATH, now=time.time, sleep=asyncio.sleep, tape_mid=None,
                 quote_wait_s: float = 0.15, verbose: bool = True):
        self.ledger = ledger
        self.http = http
        self.trade = trade                # async callable(ev, market, side, book, value, n_candidates=1)
        self.fetch_book = fetch_book
        self.cfg = cfg or settings()
        self.calendar_path = calendar_path
        self.now = now
        self.sleep = sleep
        self.tape_mid = tape_mid
        self.quote_wait_s = quote_wait_s
        self.verbose = verbose
        self.started: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._cal_error = ""
        self.closed_before: dict[str, int] = {}   # release id -> markets dropped because they close before the release
        self.requests = 0                 # GETs sent by this scheduler (any host), for --check and tests
        self.poly_trace: dict[str, dict] = {}     # release id -> what the Polymarket discovery saw (for --check)
        self.prior_check: dict[str, str] = {}     # release id -> one line about the prior_range verification
        self._fill_ts: dict[str, float | None] = {}    # release id -> first fill time of the release
        self._skipped: dict[str, int] = {}             # release id -> orders skipped by the pre-order re-checks

    def say(self, msg: str) -> None:
        if self.verbose:
            print(f"{'':16}[release] {msg}")

    # ----- bookkeeping
    def _note(self, entry: CalendarEntry, status: str, note: str = "", **extra) -> None:
        old = self.ledger.release_get(entry.release_id) or {}
        row = {"id": entry.release_id, "kind": entry.kind, "series": ",".join(series_for(entry.kind)),
               "period": entry.period, "scheduled_ts": entry.scheduled_ts(self.cfg.tz),
               "fetched_ts": old.get("fetched_ts"), "value": old.get("value"), "raw": old.get("raw"),
               "status": status, "note": note}
        row.update(extra)
        self.ledger.release_put(**row)

    def bls_used_today(self) -> int:
        return self.ledger.bls_requests(utc_day(self.now()))

    # ----- arming
    def refusal(self, entry: CalendarEntry, now: float | None = None) -> str | None:
        """Why this entry cannot arm right now (no network): a reason string, or None."""
        now = self.now() if now is None else now
        if not self.cfg.enabled:
            return "releases_disabled"
        if entry.date != datetime.fromtimestamp(now, self.cfg.tz).strftime("%Y-%m-%d"):
            return "not_today"
        done = self.ledger.release_get(entry.release_id)
        if done and done["status"] == "done":
            return "already_done"
        if done and done["status"] in ("computed", "polling", "error"):
            return "already_started"      # a restart mid-release must not re-run it and buy a second market
        if entry.kind in ("cpi", "jobs"):
            if not self.cfg.bls_key:
                return "no_bls_key"
            planned = planned_requests(entry.kind, self.cfg)
            if self.bls_used_today() + planned > self.cfg.daily_budget:
                return f"budget (used {self.bls_used_today()} + planned {planned} > {self.cfg.daily_budget})"
        return None

    async def arm(self, entry: CalendarEntry, now: float | None = None) -> ArmedRelease | None:
        """Arm one calendar entry or say why not. Refuses: not today, no key (BLS kinds), no budget headroom, no Kalshi
        market with a matching rules text for this release."""
        why = self.refusal(entry, now)
        if why:
            self.say(f"{entry.release_id}: not armed ({why})")
            return None
        markets = await self.load_markets(entry)
        if not any(markets.values()) and self.closed_before.get(entry.release_id):
            self._note(entry, "not_armed", "markets_close_before_release")
            self.say(f"{entry.release_id}: not armed (markets_close_before_release: {self.closed_before[entry.release_id]} "
                     "Kalshi markets close at or before the release)")
            return None
        if not any(markets.values()):
            self._note(entry, "not_armed", "no_markets")
            self.say(f"{entry.release_id}: not armed (no Kalshi market with a matching rules text)")
            return None
        if entry.kind == "fomc" and entry.prior_range is not None:
            ok, why = await self.verify_prior_range(entry)
            if not ok:
                self._note(entry, "not_armed", why)
                self.say(f"{entry.release_id}: not armed ({why})")
                return None
            self.say(f"{entry.release_id}: {self.prior_check.get(entry.release_id, 'prior_range verified')}")
        self._note(entry, "armed", "")
        self.say(f"{entry.release_id}: armed ({', '.join(f'{k} {len(v)}' for k, v in markets.items())} markets)")
        return ArmedRelease(entry, entry.scheduled_ts(self.cfg.tz), markets, planned_requests(entry.kind, self.cfg))

    async def verify_prior_range(self, entry: CalendarEntry) -> tuple[bool, str]:
        """The calendar's prior_range must equal the range in force before the meeting: the new range of the newest
        "FOMC statement" in the Fed feed dated strictly before the meeting day. (True, "") when they agree, else
        (False, "prior_range_mismatch: ...") or (False, "prior_range_unverified: <reason>") (no item, foreign host, GET
        error, text that does not parse). A disagreement or a doubt means the release is not armed."""
        def unverified(why: str) -> tuple[bool, str]:
            return False, f"prior_range_unverified: {why}"
        try:
            r = await self._get(FED_FEED_URL, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            items = []
            for e in feedparser.parse(r.content).entries:
                pp = e.get("published_parsed")
                if "fomc statement" not in (e.get("title") or "").lower() or not pp:
                    continue
                day = _feed_day(pp, self.cfg.tz)
                if day < entry.date:
                    items.append((day, e.get("link") or ""))
            if not items:
                return unverified("no earlier FOMC statement in the feed")
            day, link = max(items, key=lambda x: x[0])
            if urlparse(link).hostname != FED_HOST:
                return unverified("the newest earlier statement does not link to the Fed website")
            page = await self._get(link, headers={"User-Agent": USER_AGENT})
            page.raise_for_status()
            got = fomc_range(_clean(page.text))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.capture(exc, "releases.prior_range")
            return unverified(type(exc).__name__)
        if got is None:
            return unverified(f"the {day} statement did not parse to one range")
        pr = entry.prior_range
        if abs(got[0] - pr[0]) > 1e-9 or abs(got[1] - pr[1]) > 1e-9:
            return False, f"prior_range_mismatch: statement says {got[0]:g} to {got[1]:g}"
        self.prior_check[entry.release_id] = f"prior_range {pr[0]:g}-{pr[1]:g} verified against the {day} statement ({link})"
        return True, ""

    # ----- Kalshi markets
    async def _get(self, url: str, **kw) -> httpx.Response:
        self.requests += 1
        return await self.http.get(url, **kw)

    async def load_markets(self, entry: CalendarEntry) -> dict[str, list[ParsedMarket]]:
        """Open Kalshi markets of every series of the kind, parsed. Every market is stored in release_markets with the
        template it matched (or why not)."""
        out: dict[str, list[ParsedMarket]] = {}
        t_rel = entry.scheduled_ts(self.cfg.tz)
        self.closed_before[entry.release_id] = 0
        for series in series_for(entry.kind):
            out[series] = []
            if series == "KXFEDDECISION" and entry.prior_range is None:
                self.say(f"{series}: skipped (calendar entry has no prior_range)")
                continue
            if SERIES[series].venue == "polymarket":
                out[series] = await self._load_polymarket(entry, series, t_rel)
                continue
            for m in await self._series_markets(series):
                rules = m.get("rules_primary") or ""
                if rules_period(series, rules) is not None and not market_in_release(series, rules, entry):
                    continue                         # another month or meeting of the same series
                pm, why = parse_market(series, m)
                self.ledger.release_market_put(
                    market_id=m.get("ticker"), series=series, rules_primary=rules, strike_type=m.get("strike_type"),
                    floor_strike=_num(m.get("floor_strike")), cap_strike=_num(m.get("cap_strike")),
                    yes_sub_title=m.get("yes_sub_title"), template=series if pm else None,
                    parsed=pm.as_json() if pm else None, fetched_ts=self.now(), note=why or None)
                if pm is not None:
                    ct = close_ts(pm.close_time)
                    if ct is None or ct <= t_rel:
                        # Kalshi closes these ladders before the number comes out: trading them would fill against a closed book
                        self.closed_before[entry.release_id] += 1
                        continue
                    out[series].append(pm)
        return out

    async def _load_polymarket(self, entry: CalendarEntry, series: str, t_rel: float) -> list[ParsedMarket]:
        """Discover and store the Polymarket brackets of the meeting. Nothing is guessed: see poly_fomc."""
        if not self.cfg.poly_fomc:
            self.say(f"{series}: skipped (POLY_FOMC_ENABLED=false)")
            return []
        if entry.prior_range is None:
            self.say(f"{series}: skipped (calendar entry has no prior_range)")
            return []
        if entry.date[:7] != entry.period:
            self.say(f"{series}: skipped (period_date_mismatch: date {entry.date} vs period {entry.period})")
            return []
        trace: dict = {}
        self.poly_trace[entry.release_id] = trace
        try:
            accepted, rejected = await poly_fomc.discover(self.http, self._get, entry, trace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.capture(exc, "releases.polyfed")
            self.say(f"{series}: discovery failed ({type(exc).__name__}), skipped")
            return []
        trace["accepted"], trace["rejected"] = accepted, rejected
        raw = trace.get("markets", {})
        out: list[ParsedMarket] = []
        for pm in accepted:
            self.ledger.release_market_put(
                market_id=pm.ticker, series=series, rules_primary=(raw.get(pm.ticker) or {}).get("description"),
                strike_type="bracket", yes_sub_title=pm.question, template=series, parsed=pm.as_json(),
                fetched_ts=self.now(), note=None)
            ct = close_ts(pm.close_time)
            if ct is None or ct <= t_rel:
                self.closed_before[entry.release_id] += 1
                continue
            out.append(pm)
        for mid, question, why in rejected:
            if why == "ambiguous_event":
                self.say(f"{series}: more than one matching event, nothing traded")
            if not mid:
                continue
            self.ledger.release_market_put(
                market_id=mid, series=series, rules_primary=(raw.get(mid) or {}).get("description"), strike_type="bracket",
                yes_sub_title=question, template=None, parsed=None, fetched_ts=self.now(), note=why)
        return out

    async def _series_markets(self, series: str) -> list[dict]:
        out, cursor = [], ""
        for _ in range(MAX_MARKET_PAGES):
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = await self._get(f"{kalshi_api_url()}/markets", params=params)
            r.raise_for_status()
            body = r.json()
            out += body.get("markets") or []
            cursor = body.get("cursor") or ""
            if not cursor:
                break
        return out

    # ----- BLS
    async def bls_get(self, series_id: str, **params) -> dict:
        """One BLS v2 GET. The request is counted in the ledger BEFORE it is sent (a transport that fails still counts).
        Raises BudgetHit when the day's budget is used up. The key is never logged: failures are re-raised without the URL."""
        day = utc_day(self.now())
        if self.ledger.bls_requests(day) >= self.cfg.daily_budget:
            raise BudgetHit(day)
        self.ledger.bls_request_add(day)
        q = {"registrationkey": self.cfg.bls_key, **{k: v for k, v in params.items() if v is not None}}
        try:
            r = await self._get(BLS_URL.format(series=series_id), params=q, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()
        except BudgetHit:
            raise
        except Exception as exc:
            raise RuntimeError(f"BLS request for {series_id} failed: {type(exc).__name__}") from None

    async def baseline(self, entry: CalendarEntry) -> dict[str, list[Point]] | None:
        y, _ = entry.year_month
        out = {}
        for sid in bls_series_for(entry.kind):
            try:
                out[sid] = parse_bls(await self.bls_get(sid, startyear=y - 1, endyear=y))
            except BudgetHit:
                self._note(entry, "budget_hit", "baseline")
                return None
            except Exception as exc:
                errors.capture(exc, "releases.baseline")
                self._note(entry, "baseline_failed", str(exc)[:200])
                return None
            if not out[sid]:
                self._note(entry, "baseline_failed", f"{sid}: no usable data")
                return None
        return out

    def _poll_params(self, sid: str, entry: CalendarEntry) -> dict:
        if sid == "CES0000000001":      # payrolls need the prior month from the same response (revised)
            py, _ = _prev(*entry.year_month)
            return {"startyear": py, "endyear": entry.year_month[0]}
        if sid in ("CUSR0000SA0", "CUUR0000SA0"):   # CPI: t and t-1 (or t-12) from one response; January revises SA history
            return {"startyear": entry.year_month[0] - 1, "endyear": entry.year_month[0]}
        return {"latest": "true"}

    async def poll_bls(self, entry: CalendarEntry, base: dict[str, list[Point]], t_release: float):
        """Poll every needed series until each shows a period newer than its baseline; (new, status). status: ok |
        timed_out | budget_hit."""
        need = bls_series_for(entry.kind)
        newest_base = {sid: (base[sid][0].key if base[sid] else (0, 0)) for sid in need}
        new: dict[str, list[Point]] = {}
        start = t_release - self.cfg.poll_start_s
        wait = start - self.now()
        if wait > 0:
            await self.sleep(wait)
        start = self.now()
        deadline = start + self.cfg.poll_max_s
        nxt = start
        while self.now() < deadline:
            todo = [s for s in need if s not in new]
            try:
                res = await asyncio.gather(*(self.bls_get(s, **self._poll_params(s, entry)) for s in todo),
                                           return_exceptions=True)
            except BudgetHit:
                return new, "budget_hit"
            for sid, payload in zip(todo, res):
                if isinstance(payload, BudgetHit):
                    return new, "budget_hit"
                if isinstance(payload, BaseException):
                    errors.capture(payload, "releases.poll")
                    continue
                pts = parse_bls(payload)
                if pts and pts[0].key > newest_base[sid]:
                    new[sid] = pts
            if len(new) == len(need):
                return new, "ok"
            nxt = max(nxt + self.cfg.poll_every_s, self.now())     # a slow answer never causes back-to-back polls
            gap = nxt - self.now()
            if gap > 0:
                await self.sleep(gap)
        return new, "timed_out"

    # ----- running one release
    async def run_release(self, entry: CalendarEntry) -> str:
        """Arm, fetch, compute and trade one calendar entry. Returns the final status."""
        t = entry.scheduled_ts(self.cfg.tz)
        wait = t - MARKETS_LEAD_S - self.now()
        if wait > 0:
            await self.sleep(wait)
        armed = await self.arm(entry)
        if armed is None:
            return "not_armed"
        try:
            if entry.kind == "fomc":
                return await self._run_fomc(armed)
            return await self._run_bls(armed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.capture(exc, f"releases.{entry.release_id}")
            self._note(entry, "error", type(exc).__name__)
            return "error"

    async def _run_bls(self, armed: ArmedRelease) -> str:
        entry, t = armed.entry, armed.scheduled_ts
        wait = t - BASELINE_LEAD_S - self.now()
        if wait > 0:
            await self.sleep(wait)
        base = await self.baseline(entry)
        if base is None:
            return (self.ledger.release_get(entry.release_id) or {}).get("status", "baseline_failed")
        if any(pts[0].key >= entry.year_month for pts in base.values()):
            self._note(entry, "already_published", "baseline already holds the period")
            return "already_published"
        wait = t - BOOK_PREFETCH_LEAD_S - self.now()
        warm = None
        if wait > 0:
            await self.sleep(wait)
        warm = self._warm_books(armed)
        self._note(entry, "polling", "")
        new, status = await self.poll_bls(entry, base, t)
        warm.cancel()
        if status != "ok":
            self._note(entry, status, f"{len(new)}/{len(bls_series_for(entry.kind))} series")
            return status
        fetched = self.now()
        got = {sid: pts[0].key for sid, pts in new.items()}
        if any(k != entry.year_month for k in got.values()):
            self._note(entry, "period_mismatch", json.dumps(got))
            return "period_mismatch"
        values: dict[str, tuple[float, float]] = {}
        for series in series_for(entry.kind):
            r = compute_stat(series, entry.year_month, base, new)
            if r is None:
                self._note(entry, "incomplete_data", series)
                return "incomplete_data"
            values[series] = r
        raw = json.dumps({s: {"raw": v[0], "settled": v[1]} for s, v in values.items()})
        self.ledger.release_put(id=entry.release_id, kind=entry.kind, series=",".join(values), period=entry.period,
                                scheduled_ts=t, fetched_ts=fetched, value=next(iter(values.values()))[1], raw=raw,
                                status="computed", note="")
        if entry.kind == "cpi":
            unsafe = [s for s, (r, _v) in values.items() if not rounds_safely(r, 1, self.cfg.margin_cpi_pp)]
            if unsafe:
                self._note(entry, "release_margin", f"within {self.cfg.margin_cpi_pp}pp of a rounding boundary: {unsafe}",
                           fetched_ts=fetched, raw=raw)
                self.say(f"{entry.release_id}: {unsafe} too close to a rounding boundary, no trade")
                return "release_margin"
        traded = await self._trade_values(armed, {s: v[1] for s, v in values.items()}, fetched, raw)
        self._note(entry, "done", f"{traded} traded", fetched_ts=fetched, raw=raw)
        return "done"

    async def _run_fomc(self, armed: ArmedRelease) -> str:
        entry, t = armed.entry, armed.scheduled_ts
        wait = t - BOOK_PREFETCH_LEAD_S - self.now()
        if wait > 0:
            await self.sleep(wait)
        warm = self._warm_books(armed)
        wait = t - FOMC_LEAD_S - self.now()
        if wait > 0:
            await self.sleep(wait)
        self._note(entry, "polling", "")          # before the first request: a restart inside the window never re-arms
        rng, text, link, timing = await self.poll_fomc(entry, t)
        warm.cancel()
        seen = timing["seen_ts"]
        fetched = seen if seen is not None else self.now()
        if text is None:
            self._note(entry, "timed_out", "request_cap" if timing.get("capped") else "no statement in the feed")
            return "timed_out"
        if rng is None:
            self._note(entry, "parse_doubt", "statement did not parse to exactly one 25 bp range", fetched_ts=fetched)
            self.say(f"{entry.release_id}: parse_doubt, no trade")
            return "parse_doubt"
        lo, hi = rng
        info = {"range": [lo, hi], "prior_range": entry.prior_range, "url": link, "source": timing["source"],
                "statement_seen_ts": seen, "seen_lag_s": seen - t, "polls": timing["polls"], "requests": timing["requests"]}
        self.ledger.release_put(id=entry.release_id, kind=entry.kind, series=",".join(series_for(entry.kind)),
                                period=entry.period, scheduled_ts=t, fetched_ts=fetched, value=hi, raw=json.dumps(info),
                                status="computed", note="")
        if entry.prior_range is not None:
            delta = hi - entry.prior_range[1]
            frm = fomc_from_range(text)
            if (min(abs(delta - d) for d in FOMC_MOVES) > 1e-9
                    or (frm is not None and (abs(frm[0] - entry.prior_range[0]) > 1e-9
                                             or abs(frm[1] - entry.prior_range[1]) > 1e-9))):
                self._note(entry, "parse_doubt", f"prior_range {list(entry.prior_range)} disagrees with the statement "
                           f"(new range {lo}-{hi})", fetched_ts=fetched)
                self.say(f"{entry.release_id}: prior_range disagrees with the statement, no trade")
                return "parse_doubt"
        values: dict[str, float] = {"KXFED": hi}
        if entry.prior_range is not None:
            values["KXFEDDECISION"] = float(round((hi - entry.prior_range[1]) * 100))
            values["POLYFED"] = values["KXFEDDECISION"]
        decided = self.now()
        traded = await self._trade_values(armed, values, fetched, json.dumps(info), url=link)
        first = self._fill_ts.pop(entry.release_id, None)
        skipped = self._skipped.pop(entry.release_id, 0)
        info.update(decided_ts=decided, decided_lag_s=decided - t, first_fill_ts=first,
                    first_fill_lag_s=(first - t) if first is not None else None)
        raw = json.dumps(info)
        self.say(f"{entry.release_id} timing: statement seen T{seen - t:+.1f}s ({timing['source']}), decided "
                 f"T{decided - t:+.1f}s, " + (f"first fill T{first - t:+.1f}s, " if first is not None else "no fill, ")
                 + f"{timing['polls']} polls / {timing['requests']} GETs")
        self._note(entry, "done", f"{traded} traded, {skipped} skipped", fetched_ts=fetched, raw=raw, value=hi)
        return "done"

    async def poll_fomc(self, entry: CalendarEntry, t_release: float):
        """(range | None, statement text | None, link, timing). Each round asks the Fed twice: the statement URL for the
        meeting day (404 until it is posted) and the press feed (conditional GET). The first source that yields a
        statement wins. timing = {seen_ts, source ("url" | "feed" | None), polls, requests[, capped]}."""
        start = self.now()
        start_requests = self.requests
        end = start + FOMC_MAX_S
        url = FED_STATEMENT_URL.format(ymd=entry.date.replace("-", ""))
        etag = None
        polls = 0
        timing = {"seen_ts": None, "source": None, "polls": 0, "requests": 0}

        def capped() -> bool:
            return self.requests - start_requests >= FOMC_MAX_REQUESTS

        def done(rng, text, link, source):
            timing.update(seen_ts=self.now(), source=source, polls=polls, requests=self.requests - start_requests)
            return rng, text, link, timing

        nxt = start
        while self.now() < end:
            if capped():
                break
            polls += 1
            try:
                r = await self._get(url, headers={"User-Agent": USER_AGENT})
                if r.status_code == 200:
                    text = _clean(r.text)
                    if FOMC_RE.search(text):
                        return done(fomc_range(text), text, url, "url")
                elif r.status_code != 404:
                    r.raise_for_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.capture(exc, "releases.fomc")
            if capped():
                break
            try:
                h = {"User-Agent": USER_AGENT}
                if etag:
                    h["If-None-Match"] = etag
                r = await self._get(FED_FEED_URL, headers=h)
                if r.status_code == 200:
                    new_etag = r.headers.get("etag")
                    for e in feedparser.parse(r.content).entries:
                        if "fomc statement" not in (e.get("title") or "").lower():
                            continue
                        pp = e.get("published_parsed")
                        if not pp or _feed_day(pp, self.cfg.tz) != entry.date:
                            continue
                        link = e.get("link") or ""
                        if urlparse(link).hostname != FED_HOST:
                            continue
                        if capped():
                            break
                        page = await self._get(link, headers={"User-Agent": USER_AGENT})
                        page.raise_for_status()
                        text = _clean(page.text)
                        return done(fomc_range(text), text, link, "feed")
                    else:
                        etag = new_etag or etag    # remembered only once every item was handled: a failed page GET is retried
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.capture(exc, "releases.fomc")
            every = FOMC_FAST_POLL_S if self.now() < start + FOMC_FAST_WINDOW_S else FOMC_POLL_S
            nxt = max(nxt + every, self.now())
            gap = nxt - self.now()
            if gap > 0:
                await self.sleep(gap)
        timing.update(polls=polls, requests=self.requests - start_requests, capped=capped())
        return None, None, "", timing

    # ----- books and trades
    def _warm_books(self, armed: ArmedRelease) -> asyncio.Task:
        async def warm():
            for pms in armed.markets.values():
                await asyncio.gather(*(self.fetch_book(self.http, self._market(pm)) for pm in pms), return_exceptions=True)
        return asyncio.create_task(warm())

    @staticmethod
    def _market(pm: ParsedMarket) -> dict:
        if pm.venue == "polymarket":
            return {"venue": "polymarket", "id": pm.ticker, "question": pm.question, "category": "Economics",
                    "yes_token": pm.yes_token, "no_token": pm.no_token}
        return {"venue": "kalshi", "id": pm.ticker, "question": pm.question, "category": "Economics"}

    async def fresh_books(self, pms: list[ParsedMarket], wait_s: float | None = None) -> dict[str, Book]:
        """Fresh books for the candidates, waiting at most LIVE_QUOTE_WAIT_MS (the same bounded wait as the news path;
        `wait_s` overrides it). Books that did not land in time are simply not candidates."""
        tasks = {pm.ticker: asyncio.ensure_future(self.fetch_book(self.http, self._market(pm))) for pm in pms}
        if tasks:
            await asyncio.wait(set(tasks.values()), timeout=self.quote_wait_s if wait_s is None else wait_s)
        out = {}
        for tk, task in tasks.items():
            if task.done() and not task.cancelled() and task.exception() is None:
                out[tk] = task.result()
            else:
                if task.done() and not task.cancelled():
                    task.exception()
                task.cancel()
        return out

    def rank(self, entry: CalendarEntry, series: str, value: float, books: dict[str, Book],
             pms: list[ParsedMarket]) -> list[tuple[ParsedMarket, str, Book, float]]:
        """Markets worth buying for the settled `value`, most room first: [(market, side, book, entry_price)]."""
        picks = []
        for pm in pms:
            if on_boundary(pm, value) or not margin_ok(pm, value, self.cfg):
                continue
            bk = books.get(pm.ticker)
            if bk is None:
                continue
            side = "yes" if resolves_yes(pm, value) else "no"
            px = bk.best(side)
            if px is None or px >= MAX_ENTRY_PRICE:
                continue
            picks.append((pm, side, bk, px))
        picks.sort(key=lambda x: 1 - x[3], reverse=True)
        return picks[: self.cfg.max_markets_per_series]

    def rank_brackets(self, value: float, books: dict[str, Book], pms: list[ParsedMarket]
                      ) -> list[tuple[ParsedMarket, str, Book, float]]:
        """Polymarket brackets worth buying for the settled bps change `value`: the resolving bracket on YES first (it
        needs a book, a YES ask below MAX_ENTRY_PRICE and a YES bid), then up to cfg.poly_max_no dead brackets on NO, most
        room first (lowest NO ask), each needing a NO ask below MAX_ENTRY_PRICE and a NO bid. A book with no ask or no bid
        on the wanted side is one-sided and skipped. A value outside the bracket map, or a resolving bracket that is not
        among the markets, ranks nothing: a dead bracket is only known dead when the live one is."""
        if not any(resolves_yes(pm, value) for pm in pms):
            return []

        def usable(bk, side):
            ask, bid = bk.best(side), bk.bid(side)
            return ask is not None and ask < MAX_ENTRY_PRICE and bid is not None and bid > 0

        picks, nos = [], []
        for pm in pms:
            bk = books.get(pm.ticker)
            if bk is None:
                continue
            if resolves_yes(pm, value):
                if usable(bk, "yes"):
                    picks.append((pm, "yes", bk, bk.best("yes")))
            elif usable(bk, "no"):
                nos.append((pm, "no", bk, bk.best("no")))
        nos.sort(key=lambda x: x[3])
        return picks + nos[: max(self.cfg.poly_max_no, 0)]

    async def _snapshot_books(self, release_id: str, pms: list[ParsedMarket], t0: float) -> None:
        """release_books rows at +5, +30 and +60 s after the decision: what the market did after the number came out."""
        for h in RELEASE_BOOK_HORIZONS_S:
            gap = t0 + h - self.now()
            if gap > 0:
                await self.sleep(gap)
            res = await asyncio.gather(*(self.fetch_book(self.http, self._market(pm)) for pm in pms), return_exceptions=True)
            for pm, bk in zip(pms, res):
                if not isinstance(bk, BaseException):
                    self._book_row(release_id, pm, f"+{h}s", self.now(), bk)

    def _book_row(self, release_id: str, pm: ParsedMarket, label: str, ts: float, bk: Book) -> None:
        self.ledger.release_book_put(
            release_id=release_id, market_id=pm.ticker, label=label, ts=ts, yes_bid=bk.bid("yes"), yes_ask=bk.best("yes"),
            bid_qty=bk.no_asks[0][1] if bk.no_asks else None, ask_qty=bk.yes_asks[0][1] if bk.yes_asks else None)

    async def _trade_polyfed(self, armed: ArmedRelease, pms: list[ParsedMarket], value: float, fetched: float, raw: str,
                             url: str) -> tuple[int, int]:
        """YES on the resolving Polymarket bracket, NO on at most poly_max_no dead ones. Paper only. Before each order:
        the market must still accept orders and a fresh two-sided book must exist. Returns (traded, skipped)."""
        entry, t = armed.entry, armed.scheduled_ts
        books = await self.fresh_books(pms, wait_s=poly_fomc.POLY_BOOK_WAIT_S)
        now = self.now()
        for pm in pms:
            if pm.ticker in books:
                self._book_row(entry.release_id, pm, "decision", now, books[pm.ticker])
        task = asyncio.create_task(self._snapshot_books(entry.release_id, pms, now))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        traded = skipped = 0
        spec = SERIES["POLYFED"]
        for i, (pm, side, _bk, _px) in enumerate(self.rank_brackets(value, books, pms)):
            try:
                ok, why = await poly_fomc.still_open(self.http, self._get, pm.ticker)
                fresh = None
                if ok:
                    fresh = await asyncio.wait_for(self.fetch_book(self.http, self._market(pm)), poly_fomc.POLY_BOOK_WAIT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.capture(exc, "releases.polyfed.recheck")
                ok, why, fresh = False, f"recheck_failed: {type(exc).__name__}", None
            if not ok:
                self.say(f"POLYFED {pm.ticker}: skipped ({why})")
                skipped += 1
                continue
            if fresh.best(side) is None or (fresh.bid(side) or 0) <= 0:
                self.say(f"POLYFED {pm.ticker}: skipped (poly_book_one_sided)")
                skipped += 1
                continue
            if (close_ts(pm.close_time) or 0) <= self.now():
                self.say(f"POLYFED {pm.ticker}: skipped (closed)")
                skipped += 1
                continue
            ev = {"id": f"release-POLYFED-{entry.period}" + (f"-m{i}" if i else ""), "source": "release:POLYFED",
                  "headline": f"{entry.kind.upper()} {entry.period}: {spec.statistic} = {value:g}",
                  "summary": raw[:500], "url": url, "published_ts": t, "seen_ts": fetched, "synthetic": False}
            rec = await self.trade(ev, self._market(pm), side, fresh, value, n_candidates=len(pms), style="take")
            traded += 1
            self._first_fill(entry, rec)
        self._skipped[entry.release_id] = self._skipped.get(entry.release_id, 0) + skipped
        return traded, skipped

    def _first_fill(self, entry: CalendarEntry, rec) -> None:
        fts = rec.get("fill_ts") if isinstance(rec, dict) else None
        if fts is not None and self._fill_ts.get(entry.release_id) is None:
            self._fill_ts[entry.release_id] = fts

    async def _trade_values(self, armed: ArmedRelease, values: dict[str, float], fetched: float, raw: str,
                            url: str = "") -> int:
        entry, t = armed.entry, armed.scheduled_ts
        traded = 0
        for series, value in values.items():
            pms = armed.markets.get(series) or []
            if not pms:
                continue
            pms = [pm for pm in pms if (close_ts(pm.close_time) or 0) > self.now()]     # still open right now
            if not pms:
                continue
            if SERIES[series].venue == "polymarket":
                n, _skipped = await self._trade_polyfed(armed, pms, value, fetched, raw, url)
                traded += n
                continue
            books = await self.fresh_books(pms)
            picks = self.rank(entry, series, value, books, pms)
            for i, (pm, side, bk, _px) in enumerate(picks):
                if (close_ts(pm.close_time) or 0) <= self.now():
                    continue                    # closed while we were fetching books: never trade a closed market
                spec = SERIES[series]
                ev = {"id": f"release-{series}-{entry.period}" + (f"-m{i}" if i else ""), "source": f"release:{series}",
                      "headline": f"{entry.kind.upper()} {entry.period}: {spec.statistic} = {value:g}",
                      "summary": raw[:500], "url": url or BLS_URL.format(series=spec.bls[0] if spec.bls else series),
                      "published_ts": t, "seen_ts": fetched, "synthetic": False}
                self._first_fill(entry, await self.trade(ev, self._market(pm), side, bk, value, n_candidates=len(pms)))
                traded += 1
        return traded

    # ----- loop
    async def tick(self) -> float:
        """One idle-loop pass: reload the calendar (the owner may have edited it), start releases that are due.
        Returns the seconds to sleep."""
        try:
            cal = load_calendar(self.calendar_path)
            self._cal_error = ""
        except ValueError as exc:
            if str(exc) != self._cal_error:
                self._cal_error = str(exc)
                print(f"release calendar: {exc}")
            return IDLE_S
        now = self.now()
        sleep_for = IDLE_S
        for e in today_entries(cal, now, self.cfg.tz):
            if e.release_id in self.started:
                continue
            t = e.scheduled_ts(self.cfg.tz)
            if now > t + self.cfg.poll_max_s + MAX_BEHIND_S:
                continue
            if now >= t - MARKETS_LEAD_S:
                self.started.add(e.release_id)
                task = asyncio.create_task(self.run_release(e))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                sleep_for = min(sleep_for, max(0.5, t - MARKETS_LEAD_S - now))
        return sleep_for

    async def run(self) -> None:
        if not self.cfg.enabled:
            return
        try:
            while True:
                try:
                    gap = await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.capture(exc, "releases.tick")
                    gap = IDLE_S
                await self.sleep(gap)
        finally:
            for t in list(self._tasks):
                t.cancel()


def _feed_day(parsed, tz: ZoneInfo) -> str:
    """Calendar day (in tz) of a feedparser UTC time struct."""
    import calendar
    ts = calendar.timegm(parsed)
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- CLI
def _check_polyfed(sched, e: CalendarEntry, out) -> None:
    """POLYFED discovery table for --check: event slug, each market id, bracket, acceptingOrders, endDate, or why not."""
    tr = sched.poly_trace.get(e.release_id)
    if tr is None:
        out("  POLYFED: not looked up (POLY_FOMC_ENABLED=false or no prior_range)")
        return
    out(f"  POLYFED event: {', '.join(tr.get('slugs') or []) or 'none found'}")
    raw = tr.get("markets", {})
    for pm in tr.get("accepted", []):
        m = raw.get(pm.ticker, {})
        out(f"    {pm.ticker:10} {pm.bracket:8} acceptingOrders {str(m.get('acceptingOrders')).lower():5} endDate {pm.close_time}")
    for mid, q, why in tr.get("rejected", []):
        out(f"    {mid or '-':10} rejected: {why} {q[:70]}")


async def check(day: str | None, cfg: ReleaseSettings, calendar_path: Path = CALENDAR_PATH, ledger=None,
                http: httpx.AsyncClient | None = None, out=print) -> int:
    """`--check`: print the calendar for the day, what the rules texts of its Kalshi markets parse to, the BLS key and
    budget state, and (key set) one baseline fetch per BLS series. GET only; never trades. Returns the request count."""
    cal = load_calendar(calendar_path)
    tz = cfg.tz
    day = day or datetime.now(tz).strftime("%Y-%m-%d")
    entries = [e for e in cal if e.date == day]
    out(f"release calendar: {len(cal)} entries, {len(entries)} on {day}; releases {'enabled' if cfg.enabled else 'DISABLED'}")
    out(f"BLS_API_KEY: {'set' if cfg.bls_key else 'not set (CPI and jobs releases disabled)'}")
    if not entries:
        out("nothing to check for that date")
        return 0
    own_http = http is None
    http = http or httpx.AsyncClient(timeout=15.0)
    if ledger is None:
        from fastlane.ledger import Ledger
        ledger = Ledger()

    async def no_trade(*a, **k):
        raise RuntimeError("--check never trades")

    async def no_book(*a, **k):
        raise RuntimeError("--check never fetches books")

    sched = ReleaseScheduler(ledger, http, no_trade, no_book, cfg, calendar_path, verbose=False)
    try:
        out(f"BLS requests used today: {sched.bls_used_today()} of {cfg.daily_budget}")
        for e in entries:
            t = datetime.fromtimestamp(e.scheduled_ts(tz), tz).strftime("%Y-%m-%d %H:%M %Z")
            out(f"\n{e.release_id}: {e.kind} for {e.period} at {t}"
                + (f", prior range {e.prior_range[0]}-{e.prior_range[1]}" if e.prior_range else ""))
            if e.kind in ("cpi", "jobs"):
                out(f"  planned BLS requests: {planned_requests(e.kind, cfg)}")
            markets = await sched.load_markets(e)
            if sched.closed_before.get(e.release_id):
                out(f"  {sched.closed_before[e.release_id]} markets close at or before the release: not traded")
            for series in series_for(e.kind):
                pms = markets.get(series) or []
                out(f"  {series}: {len(pms)} markets with a matching rules template")
                for pm in pms[:20]:
                    out(f"    {pm.ticker:32} {pm.strike_type:8} " + (f"bracket {pm.bracket}" if pm.bracket else f"floor {pm.floor} cap {pm.cap}"))
            if e.kind == "fomc":
                _check_polyfed(sched, e, out)
                if e.prior_range is not None:
                    ok, why = await sched.verify_prior_range(e)
                    out("  prior_range check: " + (sched.prior_check.get(e.release_id, "verified") if ok else f"NOT verified, {why}"))
            if e.kind in ("cpi", "jobs") and cfg.bls_key:
                base = await sched.baseline(e)
                for sid, pts in (base or {}).items():
                    out(f"  baseline {sid}: newest {pts[0].year}-{pts[0].month:02d} = {pts[0].value}")
            elif e.kind in ("cpi", "jobs"):
                out("  no BLS key: baseline skipped")
    finally:
        if own_http:
            await http.aclose()
    out(f"\nGET requests sent: {sched.requests}")
    return sched.requests


class _VirtualClock:
    """Fake time for the rehearsal. sleep() parks the caller until every other task is idle, then time jumps to the
    earliest wake-up, so concurrent sleepers (the polling loop, the book snapshots) interleave as they would live."""

    def __init__(self, t: float):
        self.t = t
        self._heap: list = []
        self._n = 0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        self._n += 1
        heapq.heappush(self._heap, (self.t + s, self._n, fut))
        await fut

    async def drive(self, main: asyncio.Task, idle_rounds: int = 60, max_steps: int = 200000) -> None:
        for _ in range(max_steps):
            for _ in range(idle_rounds):
                await asyncio.sleep(0)
            if self._heap:
                wake, _n, fut = heapq.heappop(self._heap)
                self.t = max(self.t, wake)
                if not fut.done():
                    fut.set_result(None)
            elif main.done():
                return
        raise RuntimeError("rehearsal clock did not settle")


REHEARSE_FILES = ("meta.json", "search-2026-10.json", "event-2026-10.json", "statement-2026-09-16.html",
                  "statement-2026-07-29.html")


async def rehearse(kind: str, fixtures: Path, out=print) -> int:
    """`--rehearse fomc`: replay a whole FOMC release on a fake clock against the captured responses. No network, a
    temp ledger, nothing is sent. The captured statement is played as if it were this meeting's statement, with the
    prior range it moved from, so the bracket the engine must buy is known (meta.expected_bracket)."""
    import contextlib
    import io
    import tempfile
    if kind != "fomc":
        out(f"unknown rehearsal {kind!r} (only: fomc)")
        return 2
    fx = Path(fixtures)
    for name in REHEARSE_FILES:
        if not (fx / name).exists():
            out(f"fixture missing: {name}")
            return 2
    meta = json.loads((fx / "meta.json").read_text())
    for name in ("books",):
        if not (fx / name).is_dir():
            out(f"fixture missing: {name}/")
            return 2
    from fastlane.engine import Engine
    from fastlane.ledger import Ledger

    class _NoJev:
        async def keepalive(self):
            return None

        async def aclose(self):
            return None

        async def decide(self, *a, **k):
            raise RuntimeError("the rehearsal never calls Jev")

    tz = ZoneInfo(ET)
    entry = CalendarEntry("fomc", meta["meeting_date"], "14:00", meta["period"],
                          prior_range=tuple(meta["prior_range_for_statement"]))
    t_rel = entry.scheduled_ts(tz)
    clock = _VirtualClock(t_rel - MARKETS_LEAD_S - 10)
    transport = poly_fomc.fixture_transport(fx, clock, t_rel)
    cfg = settings({"POLY_FOMC_ENABLED": "true", "POLY_FOMC_MAX_NO": os.environ.get("POLY_FOMC_MAX_NO") or "1"})
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        ledger = Ledger(Path(td) / "ledger.db")
        calendar = Path(td) / "calendar.json"
        calendar.write_text(json.dumps({"version": 1, "events": [
            {"kind": "fomc", "date": entry.date, "time_et": entry.time_et, "period": entry.period,
             "prior_range": list(entry.prior_range)}]}))
        with contextlib.redirect_stdout(buf):
            eng = Engine(workers=1, verbose=True, ledger=ledger, jev=_NoJev())
            await eng.http.aclose()
            eng.http = httpx.AsyncClient(transport=transport)
            eng.tape_enabled = False
            async def trade(*a, **k):
                rec = dict(await eng.trade_release(*a, **k))
                if rec.get("fill_ts") is not None:
                    rec["fill_ts"] = clock()           # the engine stamps wall time: put the fill on the fake clock
                return rec

            sched = ReleaseScheduler(eng.ledger, eng.http, trade, lambda c, m: fetch_book(c, m), cfg=cfg,
                                     calendar_path=calendar, now=clock, sleep=clock.sleep, verbose=True)
            main_task = asyncio.create_task(sched.run_release(entry))
            driver = asyncio.create_task(clock.drive(main_task))
            try:
                await driver
                status = main_task.result() if main_task.done() else "unfinished"
            finally:
                for t in list(eng._bg) + list(sched._tasks) + [main_task]:
                    t.cancel()
                await asyncio.gather(*list(eng._bg), *list(sched._tasks), return_exceptions=True)
                await eng.http.aclose()
        console = buf.getvalue().splitlines()
        trades = ledger.db.execute("SELECT market_id, side, contracts, avg_price, cost, fee, entry_style, venue, book "
                                   "FROM trades ORDER BY id").fetchall()
        brackets = {mid: (json.loads(p).get("bracket") if p else None, note) for mid, p, note in ledger.db.execute(
            "SELECT market_id, parsed, note FROM release_markets WHERE series = 'POLYFED'")}
        rows = ledger.release_books(entry.release_id)
        rel = ledger.release_get(entry.release_id) or {}
        ledger.db.close()
    out("REHEARSAL (fake clock, fixture network, temp ledger; nothing is sent)")
    out(f"meeting {entry.date} 14:00 ET, release {entry.release_id}, prior range {entry.prior_range[0]:g}-"
        f"{entry.prior_range[1]:g} (the range the replayed statement moved from)")
    out("console:")
    for line in console:
        out("  " + line.strip())
    out("POLYFED brackets discovered:")
    for mid, (br, note) in sorted(brackets.items()):
        out(f"  {mid}  {br or '-':8} {('rejected: ' + note) if note else 'accepted'}")
    out(f"prior range check: {sched.prior_check.get(entry.release_id, 'not verified')}")
    out("trades (market, side, contracts, avg_price, cost, fee, entry_style):")
    for mid, side, n, px, cost, fee, style, venue, book in trades:
        out(f"  {mid} {side.upper()} {n:g} @ {px} cost ${cost:.2f} fee ${fee:.2f} {style} [{venue}, {book} book] "
            f"bracket {brackets.get(mid, ('?',))[0]}")
    out("release_books (market, label, yes_bid/yes_ask, bid_qty/ask_qty):")
    for b in rows:
        out(f"  {b['market_id']} {b['label']:8} {b['yes_bid']}/{b['yes_ask']}  {b['bid_qty']}/{b['ask_qty']}")
    out(f"release row: status {rel.get('status')}, note {rel.get('note')!r}")
    out("Polymarket trades are paper only: no Polymarket order client exists (live.gate -> paper_only_market)")
    yes = [m for m, side, *_ in trades if side == "yes"]
    if status != "done" or len(yes) != 1 or brackets.get(yes[0], (None,))[0] != meta["expected_bracket"]:
        out(f"FAIL: expected one YES trade on bracket {meta['expected_bracket']}, status {status}, "
            f"YES trades {[brackets.get(m, (None,))[0] for m in yes]}")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scheduled data releases: read-only checks")
    ap.add_argument("--check", action="store_true", help="print calendar, market parse table, BLS key and budget")
    ap.add_argument("--date", help="YYYY-MM-DD (ET); default today")
    ap.add_argument("--rehearse", choices=["fomc"], help="replay a whole release on a fake clock from captured responses "
                                                        "(no network, temp ledger, nothing sent)")
    ap.add_argument("--fixtures", help="fixture folder for --rehearse (default tests/fixtures/polymarket_fomc)")
    a = ap.parse_args(argv)
    from fastlane.config import load_env
    load_env()
    if a.rehearse:
        fixtures = Path(a.fixtures) if a.fixtures else ROOT / "tests" / "fixtures" / "polymarket_fomc"
        return asyncio.run(rehearse(a.rehearse, fixtures))
    if not a.check:
        ap.print_help()
        return 0
    try:
        cfg = settings()
        asyncio.run(check(a.date, cfg))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        print(exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
