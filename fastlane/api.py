"""Read-only API over the fast-lane ledger for the dashboard.

    uvicorn fastlane.api:app --port 8787

GET /trades  -> every paper trade with its trigger, decision timings, live sell price, P&L and price path since entry.
"""
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastlane.books import fetch_book, taker_fee_per_contract
from fastlane.decision import BUCKET_EDGES, strength_bucket
from fastlane.ledger import DB_PATH, SCHEMA, columns, mark_key
from fastlane.universe import CACHE

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="fastlane")

# Reject unexpected Host headers (DNS rebinding). "testserver" is the Starlette test client's host.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"] + [
    h.strip() for h in os.environ.get("FASTLANE_ALLOWED_HOSTS", "").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

BOOK_TTL_S = 3.0
_http: httpx.AsyncClient | None = None
_books: dict[str, tuple[float, object]] = {}
_markets: dict[str, dict] = {}
_markets_mtime = 0.0


def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():  # fresh clone: serve empty payloads instead of a 500
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        return db
    db = sqlite3.connect(f"file:{quote(str(DB_PATH))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _market(market_id: str, venue: str) -> dict:
    """Universe entry (needed for Polymarket token ids); falls back to a bare Kalshi ticker."""
    global _markets_mtime
    try:
        mtime = CACHE.stat().st_mtime if CACHE.exists() else None
        if mtime is not None and mtime != _markets_mtime:
            loaded = json.loads(CACHE.read_text())
            _markets.clear()
            _markets.update((m["id"], m) for m in loaded)
            _markets_mtime = mtime
    except (OSError, ValueError, KeyError, TypeError):
        pass  # unreadable cache: keep whatever we had, fall back to a bare ticker
    return _markets.get(market_id) or {"venue": venue, "id": market_id}


async def _book(market: dict):
    global _http
    if _http is None:
        _http = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(5.0))
    hit = _books.get(market["id"])
    if hit and time.time() - hit[0] < BOOK_TTL_S:
        return hit[1]
    try:
        book = await fetch_book(_http, market)
    except Exception:
        return hit[1] if hit else None
    _books[market["id"]] = (time.time(), book)
    return book


def _held_bid(side: str, yes_bid, yes_ask):
    """What one contract of the held side sells for right now."""
    if side == "yes":
        return yes_bid
    return round(1 - yes_ask, 4) if yes_ask is not None else None


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


BookName = Literal["all", "live", "shadow", "test"]


@app.get("/trades")
async def trades(include_synthetic: bool = True, book: BookName = "all"):
    db = _db()
    try:
        return await _trades(db, include_synthetic, book)
    finally:
        db.close()


def _opt(r: sqlite3.Row, name: str, default=None):
    """Optional column: a ledger from before 0.2.0 has not been migrated yet (the engine migrates on start)."""
    return r[name] if name in r.keys() else default


def _agg(ts: list[dict]) -> dict:
    priced = [t for t in ts if t["pnl"] is not None]
    return {"trades": len(ts),
            "invested": round(sum(t["cost"] + t["fee"] for t in ts), 2),
            "value": round(sum(t["value"] for t in priced), 2),
            "pnl": round(sum(t["pnl"] for t in priced), 2),
            "winners": sum(1 for t in priced if t["pnl"] > 0),
            "losers": sum(1 for t in priced if t["pnl"] < 0)}


def _bucket_order() -> list[str]:
    e = BUCKET_EDGES
    return [f"<{e[0]:.2f}", *(f"{lo:.2f}-{hi:.2f}" for lo, hi in zip(e, e[1:])), f"{e[-1]:.2f}+", "unknown"]


def _buckets(ts: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for t in ts:
        groups.setdefault(t["bucket"] or "unknown", []).append(t)
    return {k: _agg(groups[k]) for k in _bucket_order() if k in groups}


async def _trades(db: sqlite3.Connection, include_synthetic: bool, book: str = "all") -> dict:
    rows = db.execute(f"""
        SELECT t.*, e.headline, e.source, e.published_ts, e.seen_ts, e.url,
               d.total_ms, d.jev_ms, d.shortlist_ms, d.book_ms, d.market_conf AS strength,
               d.p_up, d.p_down, d.materiality AS p_decisive, d.mid_at_published, d.decided_ts
        FROM trades t
        JOIN events e ON e.id = t.event_id
        LEFT JOIN decisions d ON d.event_id = t.event_id
        {"" if include_synthetic else "WHERE t.synthetic = 0"}
        ORDER BY t.opened_ts DESC""").fetchall()
    # Filter before fetching order books so excluded rows cost no HTTP.
    keep = {"all": lambda r: True,
            "live": lambda r: not r["synthetic"] and not _opt(r, "shadow", 0),
            "shadow": lambda r: bool(_opt(r, "shadow", 0)),
            "test": lambda r: bool(r["synthetic"])}[book]
    rows = [r for r in rows if keep(r)]

    markets = {r["market_id"]: _market(r["market_id"], r["venue"]) for r in rows}
    books = dict(zip(markets, await asyncio.gather(*(_book(m) for m in markets.values()))))
    now = time.time()

    out = []
    for r in rows:
        side, n = r["side"], r["contracts"]
        is_shadow = bool(_opt(r, "shadow", 0))
        strength_sig = _opt(r, "signal_strength")
        path = [{"ts": r["opened_ts"], "price": r["avg_price"]}]
        for m in db.execute("SELECT ts, yes_bid, yes_ask FROM marks WHERE event_id = ? AND horizon_s > 0 ORDER BY ts",
                            (mark_key(r["event_id"], is_shadow),)):
            p = _held_bid(side, m["yes_bid"], m["yes_ask"])
            if p is not None:
                path.append({"ts": m["ts"], "price": p})
        for tk in db.execute("SELECT ts, yes_bid, yes_ask FROM ticks WHERE market_id = ? AND ts > ? ORDER BY ts",
                             (r["market_id"], r["opened_ts"])):
            path.append({"ts": tk["ts"], "price": _held_bid(side, tk["yes_bid"], tk["yes_ask"])})

        book = books.get(r["market_id"])
        now_px = None
        if book is not None:
            now_px = _held_bid(side, book.bid("yes"), book.best("yes"))
            if now_px is not None:
                path.append({"ts": now, "price": now_px})
        path.sort(key=lambda p: p["ts"])

        value = n * now_px if now_px is not None else None
        pnl = value - r["cost"] - r["fee"] if value is not None else None
        out.append({
            "id": r["id"], "event_id": r["event_id"], "synthetic": bool(r["synthetic"]),
            "shadow": is_shadow, "signal_strength": strength_sig, "signal_decisive": _opt(r, "signal_decisive"),
            "bucket": strength_bucket(strength_sig),
            "venue": r["venue"], "market_id": r["market_id"], "question": r["market_question"],
            "side": side, "contracts": n, "entry_price": r["avg_price"], "best_ask_at_entry": r["best_ask"],
            "cost": r["cost"], "fee": r["fee"], "opened_ts": r["opened_ts"],
            "now_price": now_px, "value": value, "pnl": pnl,
            "pnl_pct": (pnl / (r["cost"] + r["fee"]) * 100) if pnl is not None and r["cost"] else None,
            "move_cents": (now_px - r["avg_price"]) * 100 if now_px is not None else None,
            "path": path,
            "trigger": {"headline": r["headline"], "source": r["source"], "url": r["url"],
                        "published_ts": r["published_ts"], "seen_ts": r["seen_ts"]},
            "decision": {"total_ms": r["total_ms"], "jev_ms": r["jev_ms"], "shortlist_ms": r["shortlist_ms"],
                         "book_ms": r["book_ms"], "strength": r["strength"], "p_decisive": r["p_decisive"],
                         "decided_ts": r["decided_ts"]},
        })

    real = [t for t in out if not t["shadow"]]           # the real paper book: live + test, as before 0.2.0
    live = [t for t in real if not t["synthetic"]]
    shadow = [t for t in out if t["shadow"]]
    test = [t for t in real if t["synthetic"]]
    ms = sorted(t["decision"]["total_ms"] for t in real if t["decision"]["total_ms"])
    summary = {
        "trades": len(out), "live_trades": len(live), "shadow_trades": len(shadow), "test_trades": len(test),
        **{k: v for k, v in _agg(real).items() if k != "trades"},  # invested, value, pnl, winners, losers: real book
        "median_decision_ms": ms[len(ms) // 2] if ms else None,
        "books": {"live": _agg(live), "shadow": {**_agg(shadow), "buckets": _buckets(shadow)}, "test": _agg(test)},
        "as_of": now,
    }
    return {"summary": summary, "trades": out}


@app.get("/decisions")
async def decisions(limit: int = 40):
    """Most recent live decisions (every headline judged), with how the considered market moved afterwards."""
    limit = max(1, min(limit, 200))
    db = _db()
    try:
        return _decisions(db, limit)
    finally:
        db.close()


def _decisions(db: sqlite3.Connection, limit: int) -> dict:
    have = {"shadow_action", "shadow_reason", "shadow_market_id"} <= columns(db, "decisions")
    shadow_cols = ("d.shadow_action, d.shadow_reason, d.shadow_market_id" if have else
                   "NULL AS shadow_action, NULL AS shadow_reason, NULL AS shadow_market_id")
    rows = db.execute(f"""
        SELECT e.id, e.headline, e.source, e.url, e.published_ts, e.seen_ts,
               d.decided_ts, d.total_ms, d.jev_ms, d.action, d.reason, d.venue, d.market_id, d.market_question,
               d.market_conf AS strength, d.p_up, d.p_down, d.materiality AS p_decisive, d.mid_at_decision,
               {shadow_cols}
        FROM events e JOIN decisions d ON d.event_id = e.id
        WHERE e.synthetic = 0
        ORDER BY d.decided_ts DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        latest = db.execute("SELECT mid, horizon_s FROM marks WHERE event_id = ? AND horizon_s > 0 AND mid IS NOT NULL "
                            "ORDER BY horizon_s DESC LIMIT 1", (r["id"],)).fetchone()
        leaning_yes = (r["p_up"] or 0) >= (r["p_down"] or 0)
        move = None
        if latest and r["mid_at_decision"] is not None:
            raw = (latest["mid"] - r["mid_at_decision"]) * 100
            move = raw if leaning_yes else -raw  # positive = market moved the way Jev leaned
        m0 = db.execute("SELECT yes_ask, yes_bid FROM marks WHERE event_id = ? AND horizon_s = 0", (r["id"],)).fetchone()
        last = db.execute("SELECT yes_ask, yes_bid, horizon_s FROM marks WHERE event_id = ? AND horizon_s > 0 "
                          "AND yes_ask IS NOT NULL AND yes_bid IS NOT NULL ORDER BY horizon_s DESC LIMIT 1",
                          (r["id"],)).fetchone()
        after = None
        if r["market_id"] and m0 and last:
            if leaning_yes:
                entry, exit_px = m0["yes_ask"], last["yes_bid"]
            else:
                entry = 1 - m0["yes_bid"] if m0["yes_bid"] is not None else None
                exit_px = 1 - last["yes_ask"]
            if entry is not None and exit_px is not None:
                fees = taker_fee_per_contract(r["venue"], entry) + taker_fee_per_contract(r["venue"], exit_px)
                after = round((exit_px - entry - fees) * 100, 2)
        out.append({
            "id": r["id"], "headline": r["headline"], "source": r["source"], "url": r["url"],
            "published_ts": r["published_ts"], "seen_ts": r["seen_ts"], "decided_ts": r["decided_ts"],
            "total_ms": r["total_ms"], "jev_ms": r["jev_ms"], "action": r["action"], "reason": r["reason"],
            "venue": r["venue"], "market_id": r["market_id"], "market_question": r["market_question"],
            "lean": None if not r["market_id"] else ("yes" if leaning_yes else "no"),
            "strength": r["strength"], "p_decisive": r["p_decisive"],
            "mid_at_decision": r["mid_at_decision"], "move_cents_our_way": move,
            "move_horizon_s": latest["horizon_s"] if latest else None,
            "after_costs_cents": after, "after_costs_horizon_s": last["horizon_s"] if after is not None else None,
            "shadow_action": r["shadow_action"], "shadow_reason": r["shadow_reason"],
            "shadow_market_id": r["shadow_market_id"],
            "shadow_traded": r["shadow_reason"] in ("signal_yes", "signal_no"),
        })
    return {"decisions": out}


@app.get("/health")
async def health():
    return {"ok": True, "ledger": DB_PATH.exists()}
