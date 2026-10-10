"""Polymarket "Fed Decision in <Month>?" brackets for the scheduled FOMC release (GET only, paper only).

Kalshi closes its Fed ladders before 14:00 ET; Polymarket's five brackets (cut 50+, cut 25, no change, hike 25, hike 50+)
stay open until the end of the decision day. Everything here reads public Polymarket data (gamma search and event, the
market flags, the CLOB book via books.fetch_book) and never sends an order: there is no Polymarket order client, and
LiveTrader.gate answers paper_only_market for every venue but kalshi.

A market is accepted only when EVERY check passes (never a guess):
  * its question matches one of five templates captured from the live event, naming this meeting's month and year
  * its description names the Fed calendar page and the "upper bound" of the target range (the settlement definition)
  * outcomes are exactly ["Yes", "No"], two CLOB token ids, orders accepted, not closed, active, with an end date
The bracket is the change of the upper bound in bps, the same number releases.py computes from the statement.

Also holds the fixture transport used by `python3 -m fastlane.releases --rehearse fomc` (no network).
"""
import datetime
import json
import re
from pathlib import Path

from urllib.parse import urlparse

import httpx

from fastlane.config import kalshi_api_url
from fastlane.universe import POLY_GAMMA

POLY_CLOB = "https://clob.polymarket.com"
SEARCH_PATH = "/public-search"
EVENTS_PATH = "/events"
MARKET_PATH = "/markets/{id}"
POLY_BOOK_WAIT_S = 2.0      # bounded wait for a fresh CLOB book before a Polymarket order
BRACKETS = ("cut_50", "cut_25", "hold", "hike_25", "hike_50")
BRACKET_OF_BPS = {-50: "cut_50", -25: "cut_25", 0: "hold", 25: "hike_25", 50: "hike_50"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]

_MONTH_ALT = "|".join(MONTHS)
EVENT_TITLE_RE = re.compile(rf"^Fed Decision in (?P<month>{_MONTH_ALT})\??$", re.I)
_TAIL = rf"after the (?P<month>{_MONTH_ALT}) (?P<year>\d{{4}}) meeting\?$"
# One pattern per bracket, written from the live questions saved in tests/fixtures/polymarket_fomc/.
QUESTION_TEMPLATES: dict[str, re.Pattern] = {
    "cut_50": re.compile(rf"^Will the Fed decrease interest rates by 50\+ bps {_TAIL}"),
    "cut_25": re.compile(rf"^Will the Fed decrease interest rates by 25 bps {_TAIL}"),
    "hold": re.compile(rf"^Will there be no change in Fed interest rates {_TAIL}"),
    "hike_25": re.compile(rf"^Will the Fed increase interest rates by 25 bps {_TAIL}"),
    "hike_50": re.compile(rf"^Will the Fed increase interest rates by 50\+ bps {_TAIL}"),
}
# Case-insensitive substrings the live description contains (captured text, see the fixtures README).
DESCRIPTION_MUST_HAVE = ("federalreserve.gov/monetarypolicy/fomccalendars.htm", "upper bound")


def parse_question(q: str) -> tuple[str, int, int] | None:
    """(bracket, year, month_number) for a question that matches one template exactly, else None."""
    for bracket, rx in QUESTION_TEMPLATES.items():
        m = rx.match(q or "")
        if m:
            return bracket, int(m.group("year")), [n.lower() for n in MONTHS].index(m.group("month").lower()) + 1
    return None


def description_ok(desc: str) -> bool:
    low = (desc or "").lower()
    return all(s in low for s in DESCRIPTION_MUST_HAVE)


def _jlist(x):
    """Gamma sends outcomes and clobTokenIds as JSON-encoded strings; accept a real list too."""
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return None
    return x if isinstance(x, list) else None


def parse_poly_market(m: dict, entry):
    """(ParsedMarket, "") or (None, reason). Reasons: question_mismatch, other_meeting, description_mismatch,
    description_other_meeting, outcomes_mismatch, tokens_missing, not_accepting, no_end_date."""
    from fastlane.releases import ParsedMarket, close_ts
    q = m.get("question") or ""
    pq = parse_question(q)
    if pq is None:
        return None, "question_mismatch"
    bracket, year, month = pq
    if (year, month) != entry.year_month:
        return None, "other_meeting"
    if not description_ok(m.get("description") or ""):
        return None, "description_mismatch"
    if f"{MONTHS[month - 1]} {year} meeting".lower() not in (m.get("description") or "").lower():
        return None, "description_other_meeting"
    if _jlist(m.get("outcomes")) != ["Yes", "No"]:
        return None, "outcomes_mismatch"
    toks = _jlist(m.get("clobTokenIds"))
    if not (toks and len(toks) == 2 and all(isinstance(t, str) and t for t in toks)):
        return None, "tokens_missing"
    if not (m.get("acceptingOrders") is True and m.get("closed") is False and m.get("active") is True):
        return None, "not_accepting"
    end = m.get("endDate") or ""
    if close_ts(end) is None or m.get("id") in (None, ""):
        return None, "no_end_date"
    return ParsedMarket(ticker=str(m["id"]), series="POLYFED", strike_type="bracket", question=q, close_time=end,
                        venue="polymarket", bracket=bracket, yes_token=toks[0], no_token=toks[1]), ""


async def discover(http, get, entry, trace: dict | None = None):
    """Search for the entry's month, read the matching event by slug, parse its markets.

    Returns (accepted, rejected as (market_id, question, reason)). Two different events that both yield accepted markets
    -> ([], [("", "", "ambiguous_event")]): a guess is never traded. No matching event -> ([], []).
    `trace`, when given, is filled with {"slugs": [...], "markets": {market_id: raw gamma market}} for storing and --check."""
    month = MONTHS[entry.year_month[1] - 1]
    r = await get(POLY_GAMMA + SEARCH_PATH, params={"q": f"Fed decision in {month} {entry.year_month[0]}"})
    r.raise_for_status()
    slugs: list[str] = []
    for ev in (r.json() or {}).get("events") or []:
        tm = EVENT_TITLE_RE.match(ev.get("title") or "")
        if tm and tm.group("month").lower() == month.lower() and ev.get("slug") and ev["slug"] not in slugs:
            slugs.append(ev["slug"])
    per_event: list[tuple[list, list]] = []
    if trace is not None:
        trace["slugs"], trace["markets"] = slugs, {}
    for slug in slugs:
        er = await get(POLY_GAMMA + EVENTS_PATH, params={"slug": slug})
        er.raise_for_status()
        body = er.json()
        event = next((e for e in (body if isinstance(body, list) else [body]) if isinstance(e, dict) and e.get("slug") == slug), None)
        acc, rej = [], []
        for m in (event or {}).get("markets") or []:
            if trace is not None:
                trace["markets"][str(m.get("id") or "")] = m
            pm, why = parse_poly_market(m, entry)
            if pm is not None:
                acc.append(pm)
            else:
                rej.append((str(m.get("id") or ""), m.get("question") or "", why))
        per_event.append((acc, rej))
    if sum(1 for acc, _ in per_event if acc) > 1:
        return [], [("", "", "ambiguous_event")]
    accepted = [pm for acc, _ in per_event for pm in acc]
    if len({pm.bracket for pm in accepted}) != len(accepted):
        return [], [("", "", "ambiguous_event")]       # the same bracket twice: which one settles is a guess
    rejected = [x for _, rej in per_event for x in rej]
    return accepted, rejected


async def still_open(http, get, market_id: str) -> tuple[bool, str]:
    """(True, "") when the market still takes orders (acceptingOrders, not closed, active), else (False, reason)."""
    get = get or http.get
    r = await get(POLY_GAMMA + MARKET_PATH.format(id=market_id))
    r.raise_for_status()
    m = r.json()
    if m.get("acceptingOrders") is not True:
        return False, "not_accepting_orders"
    if m.get("closed") is not False:
        return False, "closed"
    if m.get("active") is not True:
        return False, "inactive"
    return True, ""


# ---------------------------------------------------------------- rehearsal transport (no network)
def fixture_transport(fixtures: Path, clock, t_release: float, statement_delay_s: float = 1.5) -> httpx.MockTransport:
    """Serves the captured Polymarket and Fed responses at their real URLs. The statement (direct URL and feed item)
    appears `statement_delay_s` after `t_release` by `clock()`. Everything else answers 404. The returned transport
    keeps every request it served in `.requests`."""
    from fastlane.releases import FED_FEED_URL, FED_HOST, FED_STATEMENT_URL
    fx = Path(fixtures)
    meta = json.loads((fx / "meta.json").read_text())
    meeting_ymd = meta["meeting_date"].replace("-", "")
    stmt_url = FED_STATEMENT_URL.format(ymd=meeting_ymd)
    prior_url = meta["prior_statement_url"]
    new_html = (fx / "statement-2026-09-16.html").read_bytes()
    prior_html = (fx / "statement-2026-07-29.html").read_bytes()

    def pub(day: str) -> str:
        d = datetime.date.fromisoformat(day)
        return f"{d.strftime('%a')}, {d.day} {d.strftime('%b %Y')} 18:00:00 GMT"

    def feed() -> tuple[bytes, str]:
        items = []
        if clock() >= t_release + statement_delay_s:
            items.append(("Federal Reserve issues FOMC statement", stmt_url, meta["meeting_date"]))
        items.append(("Federal Reserve issues FOMC statement", prior_url, meta["prior_statement_date"]))
        body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{pub(d)}</pubDate></item>" for t, u, d in items)
        xml = f'<?xml version="1.0"?><rss version="2.0"><channel><title>Press</title>{body}</channel></rss>'
        return xml.encode(), f'"feed-{len(items)}"'

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "gamma-api.polymarket.com":
            if path == SEARCH_PATH:
                return httpx.Response(200, content=(fx / "search-2026-10.json").read_bytes())
            if path == EVENTS_PATH:
                return httpx.Response(200, content=(fx / "event-2026-10.json").read_bytes())
            mm = re.fullmatch(r"/markets/(\d+)", path)
            if mm and (fx / "markets" / f"{mm.group(1)}.json").exists():
                return httpx.Response(200, content=(fx / "markets" / f"{mm.group(1)}.json").read_bytes())
        elif host == "clob.polymarket.com" and path == "/book":
            f = fx / "books" / f"{req.url.params.get('token_id', '')}.json"
            if f.exists() and f.parent == fx / "books":
                return httpx.Response(200, content=f.read_bytes())
        elif host == urlparse(kalshi_api_url()).hostname and path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [], "cursor": ""})      # the rehearsal is about Polymarket only
        elif host == FED_HOST:
            url = str(req.url).split("?")[0]
            if url == FED_FEED_URL:
                body, etag = feed()
                if req.headers.get("if-none-match") == etag:
                    return httpx.Response(304)
                return httpx.Response(200, content=body, headers={"etag": etag, "content-type": "application/rss+xml"})
            if url == stmt_url and clock() >= t_release + statement_delay_s:
                return httpx.Response(200, content=new_html)
            if url == prior_url:
                return httpx.Response(200, content=prior_html)
        return httpx.Response(404)

    served: list[httpx.Request] = []

    def recording(req: httpx.Request) -> httpx.Response:
        served.append(req)
        return handler(req)

    tr = httpx.MockTransport(recording)
    tr.requests = served
    return tr
