"""X posts from a fixed group of handles, through xAI's x_search tool (one Grok call per poll). Paper only.

Grok can answer from training data when the search returns nothing, so a post is accepted only if its URL handle is
in the allowed list, its snowflake id decodes to a time inside the poll window, and it has not been seen before.
Post text is untrusted third-party data: it becomes a headline and nothing else.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from fastlane.feeds import _clean
from fastlane.ledger import utc_day

XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
XAI_MODEL = "grok-4.20-0309-non-reasoning"
DEFAULT_HANDLES = ("DeItaone", "FirstSquawk", "LiveSquawk", "zerohedge", "unusual_whales",
                   "Polymarket", "Breaking911", "KobeissiLetter", "WhiteHouse", "truthsocial")
MAX_HANDLES = 10
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
POST_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{15,20})(?![0-9])")
SNOWFLAKE_EPOCH_MS = 1288834974657
DEFAULT_POLL_S = 60.0
MIN_POLL_S = 15.0                 # floor: faster polling only burns budget (each call costs ~$0.018-0.055)
DEFAULT_BUDGET_USD = 25.0
ASSUMED_CALL_COST_USD = 0.06      # charged when the response carries no usage block; also the pre-call estimate
MAX_LOOKBACK_S = 600              # never accept a post older than this, whatever the gap (engine stale limit)
CLOCK_SLACK_S = 60
CALL_TIMEOUT_S = 45.0             # calls take ~4-15 s
DEFAULT_DAYS = "Mon,Tue,Wed,Thu,Fri"
DEFAULT_HOURS = "09:00-16:30"
DEFAULT_TZ = "America/New_York"
SEEN_MAX = 2000
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_COUNT_KEYS = ("foreign", "out_of_window", "malformed", "duplicate", "empty")


@dataclass(frozen=True)
class XSettings:
    api_key: str = field(repr=False)
    handles: tuple[str, ...]
    poll_seconds: float
    budget_usd: float
    days: frozenset[int]          # Monday = 0
    start: dtime
    end: dtime
    tz: ZoneInfo


def _get(env, name: str, default: str) -> str:
    return ((env.get(name) or "").strip()) or default


def _float(env, name: str, default: float) -> float:
    raw = _get(env, name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}: {raw!r} is not a number") from None


def _hhmm(raw: str) -> dtime:
    h, m = raw.strip().split(":")
    return dtime(int(h), int(m))


def x_settings(env=None) -> XSettings | None:
    """None when XAI_API_KEY is empty (feature off). ValueError with a one-line message on any invalid value."""
    env = os.environ if env is None else env
    key = (env.get("XAI_API_KEY") or "").strip()
    if not key:
        return None
    handles: list[str] = []
    for h in _get(env, "XAI_X_HANDLES", ",".join(DEFAULT_HANDLES)).split(","):
        h = h.strip().removeprefix("@")
        if not h:
            continue
        if not HANDLE_RE.match(h):
            raise ValueError(f"XAI_X_HANDLES: {h!r} is not a valid X handle (1-15 letters, digits or underscore)")
        if h.lower() not in {x.lower() for x in handles}:
            handles.append(h)
    if not 1 <= len(handles) <= MAX_HANDLES:
        raise ValueError(f"XAI_X_HANDLES: need 1 to {MAX_HANDLES} handles, got {len(handles)}")
    poll = _float(env, "XAI_POLL_SECONDS", DEFAULT_POLL_S)
    if poll < MIN_POLL_S:
        raise ValueError(f"XAI_POLL_SECONDS: must be at least {MIN_POLL_S:.0f}")
    budget = _float(env, "XAI_DAILY_BUDGET_USD", DEFAULT_BUDGET_USD)
    if not budget > 0:
        raise ValueError("XAI_DAILY_BUDGET_USD: must be greater than 0")
    days = set()
    for d in _get(env, "XAI_WINDOW_DAYS", DEFAULT_DAYS).split(","):
        d = d.strip().lower()[:3]
        if not d:
            continue
        if d not in _DAY_NAMES:
            raise ValueError(f"XAI_WINDOW_DAYS: {d!r} is not one of Mon..Sun")
        days.add(_DAY_NAMES.index(d))
    if not days:
        raise ValueError("XAI_WINDOW_DAYS: at least one day is required")
    hours = _get(env, "XAI_WINDOW_HOURS", DEFAULT_HOURS)
    try:
        a, b = hours.split("-")
        start, end = _hhmm(a), _hhmm(b)
    except ValueError:
        raise ValueError(f"XAI_WINDOW_HOURS: {hours!r} is not HH:MM-HH:MM") from None
    if not start < end:
        raise ValueError("XAI_WINDOW_HOURS: start must be before end")
    tzname = _get(env, "XAI_WINDOW_TZ", DEFAULT_TZ)
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"XAI_WINDOW_TZ: unknown time zone {tzname!r}") from None
    return XSettings(key, tuple(handles), poll, budget, frozenset(days), start, end, tz)


_DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def days_label(days) -> str:
    """Weekdays as text: all seven -> "every day"; runs collapse with a hyphen ("Mon-Fri"), runs join with commas."""
    ds = sorted(set(days))
    if len(ds) == 7:
        return "every day"
    runs: list[list[int]] = []
    for d in ds:
        if runs and d == runs[-1][-1] + 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    return ", ".join(_DAY_LABELS[r[0]] if len(r) == 1 else f"{_DAY_LABELS[r[0]]}-{_DAY_LABELS[r[-1]]}" for r in runs)


def in_window(s: XSettings, ts: float) -> bool:
    """start <= local time < end on an allowed weekday (end exclusive)."""
    local = datetime.fromtimestamp(ts, s.tz)
    return local.weekday() in s.days and s.start <= local.time() < s.end


def decode_snowflake(post_id: int) -> float:
    return ((post_id >> 22) + SNOWFLAKE_EPOCH_MS) / 1000


def build_query(handles) -> str:
    return " OR ".join(f"from:{h}" for h in handles) + " -filter:replies"


def build_body(handles) -> dict:
    prompt = (f'Call x_keyword_search exactly once with query "{build_query(handles)}", mode Latest, limit 10. '
              "Do not call any other tool and do not open threads. From the search results only, reply with a JSON "
              'object and nothing else: {"posts": [{"url": "https://x.com/<handle>/status/<id>", "text": "<post text>"}]}. '
              'If the search returns nothing, reply {"posts": []}. Never invent posts.')
    return {"model": XAI_MODEL, "input": [{"role": "user", "content": prompt}], "max_tool_calls": 1,
            "tools": [{"type": "x_search", "allowed_x_handles": list(handles)}]}


def extract_text(payload: dict) -> str:
    """Final assistant text: payload["output_text"] if present, else every output_text part of every `message` item."""
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    for item in payload.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "".join(parts)


def call_cost_usd(payload: dict) -> float:
    """usage.cost_in_usd_ticks / 1e10; ASSUMED_CALL_COST_USD when missing or not a number."""
    try:
        ticks = payload["usage"]["cost_in_usd_ticks"]
    except (KeyError, TypeError):
        return ASSUMED_CALL_COST_USD
    if isinstance(ticks, bool) or not isinstance(ticks, (int, float)) or ticks != ticks or ticks < 0:
        return ASSUMED_CALL_COST_USD
    return ticks / 1e10


def _load_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            return None


def parse_posts(text: str, allowed, window_start: float, window_end: float, seen=()) -> tuple[list[dict], dict]:
    """([{"handle", "id", "ts", "text", "url"}], counts) in the order given.

    counts: {"accepted", "foreign", "out_of_window", "malformed", "duplicate", "empty", "parse_error"}.
    """
    counts = {"accepted": 0, **{k: 0 for k in _COUNT_KEYS}, "parse_error": False}
    data = _load_json(text)
    posts = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        counts["parse_error"] = True
        return [], counts
    canon = {h.lower(): h for h in allowed}
    got: set[int] = set()
    out: list[dict] = []
    for item in posts:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not isinstance(item.get("text"), str):
            counts["malformed"] += 1
            continue
        m = POST_URL_RE.match(item["url"].strip())
        if not m:
            counts["malformed"] += 1
            continue
        handle = canon.get(m.group(1).lower())
        if handle is None:
            counts["foreign"] += 1
            continue
        pid = int(m.group(2))
        ts = decode_snowflake(pid)
        if not window_start <= ts <= window_end:
            counts["out_of_window"] += 1
            continue
        if pid in got or pid in seen:
            counts["duplicate"] += 1
            continue
        clean = _clean(item["text"])
        if not clean:
            counts["empty"] += 1
            continue
        got.add(pid)
        counts["accepted"] += 1
        out.append({"handle": handle, "id": pid, "ts": ts, "text": clean,
                    "url": f"https://x.com/{handle}/status/{pid}"})
    return out, counts


class XFeed:
    def __init__(self, queue: asyncio.Queue, stats: dict, ledger, *, settings: XSettings | None = None,
                 client: httpx.AsyncClient | None = None, now=time.time):
        """settings=None means x_settings(); client/now are injectable for tests. No network in __init__."""
        self.queue, self.ledger, self.now = queue, ledger, now
        self.settings = settings if settings is not None else x_settings()
        self.enabled = self.settings is not None
        self.st = stats.setdefault("x", {"polls": 0, "errors": 0, "new": 0, "last_ms": None, "calls_today": 0,
                                         "spend_today_usd": 0.0, "budget_hit": False, "in_window": False,
                                         "rejected": {}})
        self.client = client
        if self.client is None and self.enabled:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=10),
                                            headers={"Authorization": f"Bearer {self.settings.api_key}"})
        self.seen: dict[int, None] = {}      # insertion ordered, pruned to SEEN_MAX
        self.last_call_ts: float | None = None
        self._warned_budget_day: str | None = None
        self._max_cost_day: str | None = None
        self._max_cost_today = 0.0           # largest single-call cost recorded today (UTC), for the pre-call estimate

    def _estimate(self, day: str) -> float:
        if self._max_cost_day != day:
            self._max_cost_day, self._max_cost_today = day, 0.0
        return max(ASSUMED_CALL_COST_USD, self._max_cost_today)

    def _note_cost(self, day: str, usd: float) -> None:
        self._estimate(day)
        self._max_cost_today = max(self._max_cost_today, usd)

    def gate(self) -> str | None:
        """'outside_window' | 'budget_hit' | None. Budget check reads the ledger (survives restarts)."""
        s, t = self.settings, self.now()
        day = utc_day(t)
        calls, usd = self.ledger.x_spend(day)
        inside = in_window(s, t)
        over = usd + self._estimate(day) > s.budget_usd
        result = "outside_window" if not inside else ("budget_hit" if over else None)
        if result == "budget_hit" and self._warned_budget_day != day:
            self._warned_budget_day = day
            print(f"warning: xAI daily budget ${s.budget_usd:.2f} reached (${usd:.2f} over {calls} calls), "
                  "X polling stops until the next UTC day")
        self.st.update(budget_hit=over, in_window=inside, calls_today=calls, spend_today_usd=usd)
        self.ledger.feed_status_set("x", connected=result is None, info={
            "enabled": True, "budget_hit": over, "in_window": inside, "calls_today": calls,
            "spend_today_usd": round(usd, 4), "budget_usd": s.budget_usd})
        return result

    async def poll_once(self) -> int:
        """One xAI call: POST, record spend, parse, emit. Returns events emitted (0 on backlog or error)."""
        s = self.settings
        t0 = self.now()
        backlog = self.last_call_ts is None or t0 - self.last_call_ts > 2 * s.poll_seconds + CLOCK_SLACK_S
        window_start = max(self.last_call_ts or (t0 - s.poll_seconds), t0 - MAX_LOOKBACK_S) - CLOCK_SLACK_S
        window_end = t0 + CLOCK_SLACK_S
        day = utc_day(t0)
        started = time.perf_counter()
        try:
            r = await self.client.post(XAI_RESPONSES_URL, json=build_body(s.handles))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise                                      # the request never reached xAI: nothing to charge
        except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError):
            # No answer: read/write timeout or a dropped connection after the request went out (most likely billed).
            # A PoolTimeout may never have been sent; it is charged anyway, conservatively, so the budget errs high.
            self.ledger.x_spend_add(day, ASSUMED_CALL_COST_USD, calls=1)
            self._note_cost(day, ASSUMED_CALL_COST_USD)
            self.st["polls"] += 1
            self.st["errors"] += 1
            raise
        self.st["polls"] += 1
        self.st["last_ms"] = round((time.perf_counter() - started) * 1000)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if r.status_code != 200:
            self.st["errors"] += 1
            if isinstance(payload.get("usage"), dict):   # xAI bills successful calls; charge only if it says so
                cost = call_cost_usd(payload)
                self.ledger.x_spend_add(day, cost, calls=1)
                self._note_cost(day, cost)
            raise RuntimeError(f"xai {r.status_code}")
        cost = call_cost_usd(payload)
        self._note_cost(day, cost)
        self.ledger.x_spend_add(day, cost, calls=1)   # before parsing: a parse error is still paid for
        posts, counts = parse_posts(extract_text(payload), s.handles, window_start, window_end, self.seen)
        self.st["rejected"] = {k: v for k, v in counts.items() if k != "accepted"}
        self.last_call_ts = t0
        emitted = 0
        for p in posts:
            self.seen[p["id"]] = None
            while len(self.seen) > SEEN_MAX:
                del self.seen[next(iter(self.seen))]
            if backlog:
                continue
            ev = {"id": f"x-{p['id']}", "source": f"x:{p['handle']}", "headline": p["text"][:280], "summary": "",
                  "url": p["url"], "published_ts": p["ts"], "seen_ts": t0}
            self.st["new"] += 1
            self.ledger.x_spend_add(day, 0.0, calls=0, posts=1)
            await self.queue.put(ev)
            emitted += 1
        return emitted

    async def run(self):
        if not self.enabled:
            return
        fails = 0
        while True:
            if self.gate():
                await asyncio.sleep(30)
                continue
            t0 = self.now()
            try:
                await self.poll_once()
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fails += 1
                wait = min(self.settings.poll_seconds * 2 ** fails, 900)
                print(f"x feed error (retry in {wait:.0f}s): {exc!r}")
            wait = min(self.settings.poll_seconds * 2 ** fails, 900) if fails else self.settings.poll_seconds
            await asyncio.sleep(max(0.0, wait - (self.now() - t0)))

    async def aclose(self):
        if self.client is not None:
            await self.client.aclose()
