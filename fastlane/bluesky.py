"""Newsroom posts on Bluesky over the Jetstream firehose (keyless), with a polling fallback. Paper only.

Only the accounts in BSKY_HANDLES, only their own top-level posts: replies and reposts are skipped.
Post text is untrusted third-party data: it becomes a headline and nothing else.
"""
import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import httpx
import orjson
import websockets

from fastlane.feeds import _clean

JETSTREAM_HOSTS = ("jetstream2.us-east.bsky.network", "jetstream1.us-east.bsky.network")   # alternate on reconnect
JETSTREAM_URL = f"wss://{JETSTREAM_HOSTS[0]}/subscribe"
PUBLIC_API = "https://public.api.bsky.app/xrpc"
POST_COLLECTION = "app.bsky.feed.post"
RKEY_RE = re.compile(r"[A-Za-z0-9._:~-]{1,512}")
DEFAULT_HANDLES = ("reuters.com", "bloomberg.com", "apnews.com", "financialtimes.com", "wsj.com", "nytimes.com",
                   "cnbc.com", "politico.com", "nbcnews.com", "washingtonpost.com")
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,252}$")
POLL_S = 15.0                 # fallback polling interval per handle
FALLBACK_FOR_S = 300.0        # poll this long after a failed socket before trying the socket again
RESOLVE_RETRY_S = 600.0       # unresolved handles are retried on each socket attempt, at most this often
HEARTBEAT_S = 30.0            # feed_status heartbeat so /health can tell a dead engine from a quiet feed
SEEN_MAX = 5000
_TRUTHY = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class BskySettings:
    enabled: bool
    handles: tuple[str, ...]


def bsky_settings(env=None) -> BskySettings:
    """BSKY_ENABLED (default true, same truthy set as SHADOW_ENABLED); BSKY_HANDLES comma list, lowercased, validated
    by HANDLE_RE, deduped, non-empty when enabled; ValueError on an invalid handle."""
    env = os.environ if env is None else env
    enabled = ((env.get("BSKY_ENABLED") or "true").strip().lower() or "true") in _TRUTHY
    raw = (env.get("BSKY_HANDLES") or "").strip() or ",".join(DEFAULT_HANDLES)
    handles: list[str] = []
    for h in raw.split(","):
        h = h.strip().lower().removeprefix("@")
        if not h:
            continue
        if not HANDLE_RE.match(h):
            raise ValueError(f"BSKY_HANDLES: {h!r} is not a valid Bluesky handle")
        if h not in handles:
            handles.append(h)
    if enabled and not handles:
        raise ValueError("BSKY_HANDLES: at least one handle is required")
    return BskySettings(enabled, tuple(handles))


def jetstream_url(dids, host: str | None = None) -> str:
    """wss://<host>/subscribe (default JETSTREAM_URL) + '?wantedCollections=app.bsky.feed.post' + '&wantedDids=<did>'
    per did (urlencoded). Pure: the caller picks the host."""
    q = f"wantedCollections={POST_COLLECTION}"
    base = JETSTREAM_URL if host is None else f"wss://{host}/subscribe"
    return base + "?" + q + "".join("&" + urlencode({"wantedDids": d}) for d in dids)


def iso_ts(s) -> float | None:
    """ISO-8601 to epoch seconds; accepts 'Z', offsets, and fractional seconds of any length (trim to 6). None if bad."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip()
    if t[-1] in "Zz":
        t = t[:-1] + "+00:00"
    t = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), t, count=1)
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def post_event(handle: str, did: str, rkey: str, record: dict, seen_ts: float, fallback_ts: float | None) -> dict | None:
    """The event dict for one own top-level post, or None for a reply, empty text, or a non-post record."""
    if not isinstance(rkey, str) or not RKEY_RE.fullmatch(rkey) or rkey in (".", ".."):
        return None
    if not isinstance(record, dict) or record.get("$type", POST_COLLECTION) != POST_COLLECTION:
        return None
    if "reply" in record:
        return None
    text = _clean(record.get("text") if isinstance(record.get("text"), str) else "")[:280]
    if not text:
        return None
    created = iso_ts(record.get("createdAt"))
    published = min(created if created is not None else (fallback_ts if fallback_ts is not None else seen_ts), seen_ts)
    uri = f"at://{did}/{POST_COLLECTION}/{rkey}"
    return {"id": f"bsky-{hashlib.sha1(uri.encode()).hexdigest()[:16]}", "source": f"bsky:{handle}",
            "headline": text, "summary": "", "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
            "published_ts": published, "seen_ts": seen_ts}


def parse_jetstream(msg: dict, dids: dict[str, str], seen_ts: float) -> dict | None:
    """dids: did -> configured handle. None for anything but a `commit` `create` of a post by a wanted did."""
    if not isinstance(msg, dict) or msg.get("kind") != "commit":
        return None
    commit = msg.get("commit")
    if (not isinstance(commit, dict) or commit.get("operation") != "create"
            or commit.get("collection") != POST_COLLECTION):
        return None
    did = msg.get("did")
    if did not in dids or not isinstance(commit.get("rkey"), str):
        return None
    t = msg.get("time_us")
    fallback = t / 1e6 if isinstance(t, (int, float)) and not isinstance(t, bool) else None
    return post_event(dids[did], did, commit["rkey"], commit.get("record") or {}, seen_ts, fallback)


def parse_author_feed(payload: dict, handle: str, did: str, seen_ts: float) -> list[dict]:
    """getAuthorFeed items -> events, newest first as given. Skips reposts (item has `reason`), replies, other authors."""
    out: list[dict] = []
    for item in (payload or {}).get("feed", []) or []:
        try:
            if item.get("reason") or item.get("reply"):
                continue
            post = item["post"]
            if post["author"]["did"] != did:
                continue
            rkey = post["uri"].rsplit("/", 1)[-1]
            ev = post_event(handle, did, rkey, post.get("record") or {}, seen_ts, iso_ts(post.get("indexedAt")))
        except (KeyError, TypeError, AttributeError):
            continue
        if ev:
            out.append(ev)
    return out


class BlueskyFeed:
    def __init__(self, queue, stats, ledger, *, settings: BskySettings | None = None,
                 client: httpx.AsyncClient | None = None, ws_connect=websockets.connect, now=time.time):
        self.queue, self.ledger, self.ws_connect, self.now = queue, ledger, ws_connect, now
        self.settings = settings if settings is not None else bsky_settings()
        self.enabled = self.settings.enabled
        self.st = stats.setdefault("bsky", {"polls": 0, "errors": 0, "new": 0, "last_ms": None,
                                            "mode": "idle", "connected": False, "reconnects": 0})
        self.client = client
        if self.client is None and self.enabled:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self.dids: dict[str, str] = {}        # did -> handle
        self.seen: dict[str, None] = {}       # at-uri hash, pruned to SEEN_MAX
        self.connected = False
        self.mode = "idle"                    # "idle" (before start) | "ws" | "poll"
        self._poll_backlog: set[str] = set()
        self._last_resolve = 0.0
        self._warned_none = False

    def _set_state(self, mode: str, connected: bool):
        self.mode, self.connected = mode, connected
        self.st.update(mode=mode, connected=connected)
        self.ledger.feed_status_set("bsky", connected, {"mode": mode})

    @staticmethod
    def _close_info(exc: BaseException) -> str:
        """repr plus the close code/reason when the exception carries a close frame (either direction)."""
        frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
        if frame is None:
            return f"{exc!r} (no close frame)"
        return f"{exc!r} (close code {getattr(frame, 'code', None)}, reason {getattr(frame, 'reason', '')!r})"

    def _remember(self, ev_id: str) -> bool:
        """True when new. Bounded."""
        if ev_id in self.seen:
            return False
        self.seen[ev_id] = None
        while len(self.seen) > SEEN_MAX:
            del self.seen[next(iter(self.seen))]
        return True

    async def resolve(self) -> dict[str, str]:
        """GET {PUBLIC_API}/com.atproto.identity.resolveHandle?handle=<h> for each unresolved handle; failures print
        one line per handle and are retried on the next call (at most every RESOLVE_RETRY_S)."""
        have = set(self.dids.values())
        missing = [h for h in self.settings.handles if h not in have]
        if not missing or (self._last_resolve and self.now() - self._last_resolve < RESOLVE_RETRY_S
                           and self.dids):
            return self.dids
        self._last_resolve = self.now()
        for h in missing:
            try:
                r = await self.client.get(f"{PUBLIC_API}/com.atproto.identity.resolveHandle?handle={quote(h)}")
                if r.status_code != 200:
                    raise RuntimeError(f"http {r.status_code}")
                did = r.json().get("did")
                if not isinstance(did, str) or not did.startswith("did:"):
                    raise RuntimeError("no did in response")
                self.dids[did] = h
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"bluesky: could not resolve {h}: {exc!r}")
        return self.dids

    async def poll_once(self, did: str, handle: str) -> int:
        """One getAuthorFeed GET for one handle. First poll per handle since entering poll mode = backlog."""
        t0 = time.perf_counter()
        url = f"{PUBLIC_API}/app.bsky.feed.getAuthorFeed?actor={quote(handle)}&limit=20&filter=posts_no_replies"
        r = await self.client.get(url)
        self.st["polls"] += 1
        self.st["last_ms"] = round((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            self.st["errors"] += 1
            return 0
        first = handle not in self._poll_backlog
        self._poll_backlog.add(handle)
        emitted = 0
        for ev in parse_author_feed(r.json(), handle, did, self.now()):
            if not self._remember(ev["id"]) or first:
                continue
            self.st["new"] += 1
            await self.queue.put(ev)
            emitted += 1
        return emitted

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self.ledger.feed_status_set("bsky", True, {"mode": "ws"})

    async def run(self):
        if not self.enabled:
            return
        backoff = 1
        attempt = 0
        while True:
            await self.resolve()
            if not self.dids:
                if not self._warned_none:
                    self._warned_none = True
                    print("bluesky: no handle resolved, retrying in 60s")
                self._last_resolve = 0.0
                await asyncio.sleep(60)
                continue
            beat = None
            host = JETSTREAM_HOSTS[attempt % len(JETSTREAM_HOSTS)]
            attempt += 1
            try:
                async with self.ws_connect(jetstream_url(list(self.dids), host), max_size=2**22,
                                           ping_interval=20, ping_timeout=30) as ws:
                    self._poll_backlog.clear()
                    self._set_state("ws", True)
                    beat = asyncio.create_task(self._heartbeat())
                    got_msg = False
                    async for raw in ws:
                        seen_ts = self.now()
                        self.st["polls"] += 1
                        if not got_msg:
                            got_msg, backoff = True, 1   # a healthy stream: only now forget past failures
                        try:
                            ev = parse_jetstream(orjson.loads(raw), self.dids, seen_ts)
                        except orjson.JSONDecodeError:
                            continue
                        if ev is None or not self._remember(ev["id"]):
                            continue
                        self.st["new"] += 1
                        await self.queue.put(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"bluesky socket down, polling getAuthorFeed every {POLL_S:.0f}s for "
                      f"{FALLBACK_FOR_S / 60:.0f} min (host {host}): {self._close_info(exc)}")
            finally:
                if beat:
                    beat.cancel()
            self.st["reconnects"] += 1
            self._set_state("poll", False)
            deadline = self.now() + FALLBACK_FOR_S
            while self.now() < deadline:
                errors_before = self.st["errors"]
                for did, handle in list(self.dids.items()):
                    try:
                        await self.poll_once(did, handle)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.st["errors"] += 1
                    # heartbeat after each handle so a slow failing round never looks stale
                    self._set_state("poll", self.st["errors"] == errors_before)
                # Heartbeat in poll mode too (every round, under HEARTBEAT_S apart): connected while polls succeed.
                self._set_state("poll", self.st["errors"] == errors_before)
                await asyncio.sleep(POLL_S)
            backoff = min(backoff * 2, 30)
            await asyncio.sleep(backoff)

    async def aclose(self):
        if self.client is not None:
            await self.client.aclose()
