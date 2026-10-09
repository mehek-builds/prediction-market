"""Open prediction-market universe with an IDF-weighted inverted index for millisecond shortlisting."""
import asyncio
import json
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import httpx

from fastlane.config import RESULTS_DIR

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
CACHE = RESULTS_DIR / "universe.json"
CACHE_MAX_AGE_S = 30 * 60
SKIP_KALSHI_CATEGORIES = {"Sports", "Mentions"}
MENTION_RE = re.compile(r"^what will .{1,80}? say\b", re.I)
# Short-cycle markets (15-minute crypto/commodity targets, "up or down") reprice constantly by design: their moves are
# not news, and RSS-speed news can never beat their expiry.
SHORT_CYCLE_RE = re.compile(r"\b15 min|\bup or down\b", re.I)
MAX_MARKETS_PER_EVENT = 25
POLY_PAGE = 100  # gamma caps a page at 100
POLY_PAGES = 21  # gamma rejects offsets past 2,100

STOP = set("""
the a an of to in on for and or is are be will by at with from as its it this that than after over before into up down
new says said more less what who how why vs not have has had was were been being would could should may might can
next any all one two three first last least most much many other than then there their they them out about against
between during under above below win wins won yes no end year years month months day days week weeks time today
2025 2026 2027 2028 2029 2030 2031 2032 2035 2040 2050 2099 jan feb mar apr may jun jul aug sep oct nov dec january february march
april june july august september october november december inc corp corporation co company ltd llc plc holdings group
filer live updates update report reports reported price prices market markets stock stocks share shares percent
prediction repriced within seconds just
""".split())


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9&.'-]*[a-z0-9]|[a-z0-9]", text.lower().replace("’", "'"))
            if len(w) > 2 and w not in STOP and not w.isdigit()]


class Universe:
    def __init__(self):
        self.markets: list[dict] = []
        self.index: dict[str, list[int]] = {}
        self.idf: dict[str, float] = {}
        self.loaded_at = 0.0
        self.snapshot_ts = 0.0  # when the quotes in self.markets were fetched

    # ---------- loading ----------
    async def load(self, client: httpx.AsyncClient, force: bool = False):
        if not force and CACHE.exists() and time.time() - CACHE.stat().st_mtime < CACHE_MAX_AGE_S:
            self._set(json.loads(CACHE.read_text()))
            self.snapshot_ts = CACHE.stat().st_mtime
            return "cache"
        kalshi, poly = await asyncio.gather(self._kalshi(client), self._poly(client))
        markets = kalshi + poly
        if markets:
            CACHE.parent.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=CACHE.parent, prefix=CACHE.name + ".", suffix=".tmp",
                                             delete=False) as f:
                f.write(json.dumps(markets))
            os.replace(f.name, CACHE)
            self._set(markets)
            self.snapshot_ts = time.time()
        return "network"

    async def _kalshi(self, client) -> list[dict]:
        out, cursor = [], None
        try:
            while True:
                params = {"status": "open", "with_nested_markets": "true", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                r = await client.get(f"{KALSHI}/events", params=params)
                r.raise_for_status()
                data = r.json()
                for ev in data.get("events", []):
                    title = ev.get("title", "")
                    if (ev.get("category") in SKIP_KALSHI_CATEGORIES or MENTION_RE.match(title)
                            or SHORT_CYCLE_RE.search(title)):
                        continue
                    for m in (ev.get("markets") or [])[:MAX_MARKETS_PER_EVENT]:
                        if float(m.get("volume_fp") or 0) <= 0 or m.get("status") != "active":
                            continue
                        sub = m.get("yes_sub_title") or ""
                        out.append({
                            "venue": "kalshi", "id": m["ticker"],
                            "question": f"{ev.get('title', '')}: {sub}" if sub else ev.get("title", ""),
                            "category": ev.get("category") or "",
                            "yes_ask": float(m.get("yes_ask_dollars") or 0),
                            "yes_bid": float(m.get("yes_bid_dollars") or 0),
                            "volume_24h": float(m.get("volume_24h_fp") or 0),
                        })
                cursor = data.get("cursor")
                if not cursor or not data.get("events"):
                    break
        except Exception as exc:
            print(f"  kalshi universe partial ({len(out)}): {exc}")
        return out

    async def _poly(self, client) -> list[dict]:
        out = []
        try:
            for page in range(POLY_PAGES):
                r = await client.get(f"{POLY_GAMMA}/markets", params={
                    "active": "true", "closed": "false", "limit": POLY_PAGE, "offset": page * POLY_PAGE,
                    "order": "volume24hr", "ascending": "false"})
                r.raise_for_status()
                batch = r.json()
                for m in batch:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    if len(toks) != 2 or not m.get("enableOrderBook", True):
                        continue
                    out.append({
                        "venue": "polymarket", "id": str(m["id"]), "question": m.get("question", ""),
                        "category": m.get("category") or "", "yes_token": toks[0], "no_token": toks[1],
                        "yes_ask": float(m.get("bestAsk") or 0), "yes_bid": float(m.get("bestBid") or 0),
                        "volume_24h": float(m.get("volume24hr") or 0),
                    })
                if len(batch) < POLY_PAGE:
                    break
        except Exception as exc:
            print(f"  polymarket universe partial ({len(out)}): {exc}")
        return out

    def _set(self, markets: list[dict]):
        index = defaultdict(list)
        for i, m in enumerate(markets):
            for t in set(tokens(m["question"])):
                index[t].append(i)
        n = max(len(markets), 1)
        self.idf = {t: math.log(n / len(ids)) for t, ids in index.items()}
        self.index = dict(index)
        self.markets = markets
        self.loaded_at = time.time()

    # ---------- matching ----------
    def shortlist(self, text: str, k: int = 8, min_score: float = 6.0) -> list[dict]:
        """Markets sharing rare words with the text. Score = sum of IDF of shared tokens.

        min_score ~6 means roughly one genuinely rare word (a company or person name) or two mid-rare ones.
        Collapses multi-strike ladders (e.g. 20 "CPI above X%" strikes) to the 2 most liquid per event key.
        """
        scores = defaultdict(float)
        for t in set(tokens(text)):
            for i in self.index.get(t, ()):
                scores[i] += self.idf[t]
        ranked = sorted(((s, i) for i, s in scores.items() if s >= min_score),
                        key=lambda x: (-x[0], -self.markets[x[1]]["volume_24h"]))
        out, per_event = [], defaultdict(int)
        for s, i in ranked:
            m = self.markets[i]
            ev_key = m["question"].split(":")[0]
            if per_event[ev_key] >= 2:
                continue
            per_event[ev_key] += 1
            out.append({**m, "match_score": round(s, 1)})
            if len(out) >= k:
                break
        return out
