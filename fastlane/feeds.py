"""News pollers. Each emits events onto an asyncio.Queue the moment a new item appears.

The first poll of every feed is treated as backlog and never traded: those prices already reflect it.
"""
import asyncio
import calendar
import hashlib
import html
import os
import re
import time

import feedparser
import httpx

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# name: (url, minimum poll seconds). The poller also honours each response's Cache-Control max-age (capped at
# MAX_POLL_S): polling faster than the CDN refreshes returns the same bytes, so it only wastes requests.
MAX_POLL_S = 30
FEEDS = {
    "cnbc_top": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", 2),
    "cnbc_economy": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", 2),
    "cnbc_earnings": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135", 2),
    "cnbc_world": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", 2),
    "cnbc_politics": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000113", 2),
    "cnbc_tech": ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910", 2),
    "marketwatch": ("https://feeds.content.dowjones.io/public/rss/mw_topstories", 3),
    "mw_bulletins": ("https://feeds.content.dowjones.io/public/rss/mw_bulletins", 3),
    "fed_press": ("https://www.federalreserve.gov/feeds/press_all.xml", 3),
    "whitehouse": ("https://www.whitehouse.gov/news/feed/", 10),
    "businesswire": ("https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA==", 8),
    "prnewswire": ("https://www.prnewswire.com/rss/news-releases-list.rss", 60),  # rate-limits hard polling
    "bbc_world": ("https://feeds.bbci.co.uk/news/world/rss.xml", 2),
    "bbc_business": ("https://feeds.bbci.co.uk/news/business/rss.xml", 2),
    "nyt_business": ("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", 10),
    "nyt_politics": ("https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", 10),
    "coindesk": ("https://www.coindesk.com/arc/outboundfeeds/rss/", 10),
}

EDGAR_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
             "&owner=include&start=0&count=40&output=atom")
EDGAR_POLL_S = 1  # SEC fair access allows 10 req/s; 1 req/s is well inside it (EDGAR sends no-cache)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", text or ""))).strip()


def _ts(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            return float(calendar.timegm(entry[key]))
    return None


class FeedHub:
    def __init__(self, queue: asyncio.Queue, stats: dict):
        self.queue = queue
        self.stats = stats  # name -> {"polls", "errors", "new", "last_ms"}
        self.seen: set[str] = set()
        self.client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(12.0, connect=5.0),
                                        follow_redirects=True, headers={"User-Agent": BROWSER_UA})
        self.sec_ua = os.environ.get("SEC_USER_AGENT", "").strip()
        self._warned_sec = False

    def _key(self, source, entry) -> str:
        raw = entry.get("id") or entry.get("link") or entry.get("title", "")
        return hashlib.sha1(f"{raw}".encode()).hexdigest()[:16]

    async def _poll(self, name: str, url: str, every: float, headers: dict | None = None, parse=None):
        st = self.stats.setdefault(name, {"polls": 0, "errors": 0, "new": 0, "last_ms": None})
        etag = last_mod = None
        first = True
        fails = 0  # consecutive failures -> exponential backoff so a struggling source is not hammered
        while True:
            t0 = time.perf_counter()
            r = None
            try:
                h = dict(headers or {})
                if etag:
                    h["If-None-Match"] = etag
                if last_mod:
                    h["If-Modified-Since"] = last_mod
                r = await self.client.get(url, headers=h)
                st["polls"] += 1
                st["last_ms"] = round((time.perf_counter() - t0) * 1000)
                if r.status_code == 200:
                    etag, last_mod = r.headers.get("etag"), r.headers.get("last-modified")
                    seen_ts = time.time()
                    fails = 0
                    # Parse off the event loop: big feeds take tens of ms and would delay in-flight decisions.
                    entries = (await asyncio.to_thread(feedparser.parse, r.content)).entries
                    for entry in entries:
                        key = self._key(name, entry)
                        if key in self.seen:
                            continue
                        self.seen.add(key)
                        if first:
                            continue  # backlog: already priced in
                        ev = (parse or self._default)(name, entry)
                        ev.update(id=key, seen_ts=seen_ts)
                        st["new"] += 1
                        await self.queue.put(ev)
                    first = False
                elif r.status_code == 304:
                    fails = 0
                else:
                    st["errors"] += 1
                    fails += 1
            except Exception:
                st["errors"] += 1
                fails += 1
            interval = every
            m = re.search(r"max-age=(\d+)", (r.headers.get("cache-control") or "") if r is not None else "")
            if m:
                interval = max(every, min(int(m.group(1)), MAX_POLL_S))
            if fails:
                interval = min(every * 2 ** fails, 300)
            st["interval"] = interval
            await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    @staticmethod
    def _default(name, entry) -> dict:
        return {"source": name, "headline": _clean(entry.get("title", "")),
                "summary": _clean(entry.get("summary", ""))[:500], "url": entry.get("link", ""),
                "published_ts": _ts(entry)}

    @staticmethod
    def _edgar(name, entry) -> dict:
        # "8-K - AVIENT CORP (0001122976) (Filer)" + item list -> readable headline
        company = re.sub(r"^8-K\S*\s*-\s*|\s*\(\d+\)\s*\(.*?\)\s*$", "", entry.get("title", "")).strip()
        items = re.findall(r"Item \d+\.\d+:\s*([^<\n]+)", html.unescape(entry.get("summary", "")))
        headline = f"{company} files 8-K: " + "; ".join(i.strip() for i in items[:3]) if items else f"{company} files 8-K"
        return {"source": name, "headline": headline, "summary": "", "url": entry.get("link", ""),
                "published_ts": _ts(entry)}

    def specs(self) -> list[tuple]:
        """(name, url, every_s, headers, parse) for every poller that should run."""
        out = [(n, u, s, None, None) for n, (u, s) in FEEDS.items()]
        if self.sec_ua:
            out.append(("edgar_8k", EDGAR_URL, EDGAR_POLL_S, {"User-Agent": self.sec_ua}, self._edgar))
        elif not self._warned_sec:
            self._warned_sec = True
            print('warning: SEC_USER_AGENT not set, EDGAR 8-K poller disabled '
                  '(SEC requires a contact string, e.g. "name email@example.com")')
        return out

    def tasks(self) -> list[asyncio.Task]:
        return [asyncio.create_task(self._poll(n, u, s, headers=h, parse=p)) for n, u, s, h, p in self.specs()]

    async def aclose(self):
        await self.client.aclose()


async def fetch_snapshot(client: httpx.AsyncClient) -> list[dict]:
    """One-shot GET of every FEEDS url (no EDGAR), parsed with FeedHub._default, deduped by lowercase headline.

    A failing feed prints one line and is skipped.
    """
    async def one(name: str, url: str) -> list[dict]:
        try:
            r = await client.get(url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True)
            entries = feedparser.parse(r.content).entries
            return [ev for ev in (FeedHub._default(name, e) for e in entries) if ev["headline"]]
        except Exception as exc:  # one dead feed should not kill the run
            print(f"  feed {name} failed: {exc}")
            return []

    batches = await asyncio.gather(*(one(n, u) for n, (u, _) in FEEDS.items()))
    seen: set[str] = set()
    out: list[dict] = []
    for item in (i for b in batches for i in b):
        key = item["headline"].lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
