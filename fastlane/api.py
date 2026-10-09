"""Read-only API over the fast-lane ledger for the dashboard.

    uvicorn fastlane.api:app --port 8787

GET /trades  -> every paper trade with its trigger, decision timings, live sell price, P&L and price path since entry.
GET /decisions, /status, /settings, /health -> read-only views of the ledger.
POST /settings/shadow -> the only write: toggles shadow mode at runtime (see README).
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
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastlane.books import fetch_book, taker_fee_per_contract
from fastlane.decision import BUCKET_EDGES, strength_bucket
from fastlane.ledger import DB_PATH, SCHEMA, SETTINGS_DDL, columns, mark_key, shadow_enabled_from, utc_day
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
               d.decided_ts, d.total_ms, d.jev_ms, d.shortlist_ms, d.book_ms, d.n_candidates, d.action, d.reason, d.venue, d.market_id, d.market_question,
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
            "total_ms": r["total_ms"], "jev_ms": r["jev_ms"], "shortlist_ms": r["shortlist_ms"],
            "book_ms": r["book_ms"], "n_candidates": r["n_candidates"],
            "action": r["action"], "reason": r["reason"],
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
    db = _db()
    try:
        return {"ok": True, "ledger": DB_PATH.exists(), **_sources(db)}
    finally:
        db.close()


STALE_STATUS_S = 120


def _sources(db: sqlite3.Connection) -> dict:
    """x: calls and spend for today's UTC day from x_spend, flags from feed_status('x'); bluesky: from feed_status.

    A ledger from before 0.3.0 (opened read-only, not yet migrated by the engine) has neither table: return defaults.
    A feed_status row older than STALE_STATUS_S (120) counts as not connected (the engine is not running).
    x.enabled reports configuration (the key is set), so it stays true through a long error backoff; x.budget_hit and
    x.in_window are heartbeat-based and read false when the row is stale.
    """
    x = {"enabled": False, "calls_today": 0, "spend_today_usd": 0.0, "budget_hit": False, "in_window": False,
         "updated_ts": None}
    b = {"connected": False, "mode": None, "updated_ts": None}

    def status(name):
        try:
            row = db.execute("SELECT connected, updated_ts, info FROM feed_status WHERE name = ?", (name,)).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        try:
            info = json.loads(row[2] or "{}")
        except ValueError:
            info = {}
        fresh = row[1] is not None and time.time() - row[1] <= STALE_STATUS_S
        return bool(row[0]) and fresh, row[1], info if isinstance(info, dict) else {}

    try:
        row = db.execute("SELECT calls, usd FROM x_spend WHERE day = ?", (utc_day(),)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        x["calls_today"], x["spend_today_usd"] = int(row[0]), float(row[1])
    sx = status("x")
    if sx:
        _, ts, info = sx
        fresh = ts is not None and time.time() - ts <= STALE_STATUS_S
        x.update(enabled=bool(info.get("enabled")), budget_hit=bool(info.get("budget_hit")) and fresh,
                 in_window=bool(info.get("in_window")) and fresh, updated_ts=ts)
    sb = status("bsky")
    if sb:
        conn, ts, info = sb
        b.update(connected=conn, mode=info.get("mode"), updated_ts=ts)
    return {"x": x, "bluesky": b}


@app.get("/status")
async def status():
    """Engine liveness as seen from the ledger. The API has no link to the running process, so every field is a
    timestamp or a count the dashboard turns into "N ago"; it never claims up/down. The v0.3.0 `x` and `bluesky`
    blocks of /health are repeated here so the masthead needs one call."""
    db = _db()
    try:
        return _status(db)
    finally:
        db.close()


def _status(db: sqlite3.Connection) -> dict:
    now = time.time()
    hour = now - 3600

    def one(sql: str, *args):
        r = db.execute(sql, args).fetchone()
        return r[0], int(r[1] or 0)

    last_event, events_1h = one("SELECT MAX(seen_ts), SUM(seen_ts > ?) FROM events WHERE synthetic = 0", hour)
    last_decision, decisions_1h = one(
        "SELECT MAX(d.decided_ts), SUM(d.decided_ts > ?) FROM decisions d JOIN events e ON e.id = d.event_id "
        "WHERE e.synthetic = 0", hour)
    last_trade = db.execute("SELECT MAX(opened_ts) FROM trades WHERE synthetic = 0").fetchone()[0]
    last_tick, ticks_1h = one("SELECT MAX(ts), SUM(ts > ?) FROM ticks", hour)
    sources = [{"source": r[0], "last_seen_ts": r[1], "events_1h": int(r[2] or 0)} for r in db.execute(
        "SELECT source, MAX(seen_ts), SUM(seen_ts > ?) FROM events WHERE synthetic = 0 GROUP BY source "
        "ORDER BY 2 DESC", (hour,))]
    x = _sources(db)
    budget = None
    try:
        row = db.execute("SELECT info FROM feed_status WHERE name = 'x'").fetchone()
        info = json.loads(row[0] or "{}") if row else {}
        if isinstance(info, dict) and isinstance(info.get("budget_usd"), (int, float)):
            budget = float(info["budget_usd"])
    except (sqlite3.OperationalError, ValueError):
        pass
    x["x"]["budget_usd"] = budget      # None until the engine has written a heartbeat that carries it
    return {"now": now, "ledger": DB_PATH.exists(),
            "last_event_ts": last_event, "last_decision_ts": last_decision, "last_trade_ts": last_trade,
            "last_tick_ts": last_tick, "events_1h": events_1h, "decisions_1h": decisions_1h, "ticks_1h": ticks_1h,
            "sources": sources, **x}


@app.get("/settings")
async def settings():
    """Current runtime settings and where each comes from. Only shadow mode is settable (POST /settings/shadow)."""
    db = _db()
    try:
        enabled, source = shadow_enabled_from(db)
    except sqlite3.OperationalError:   # locked / busy: transient, the dashboard keeps its previous settings
        return JSONResponse({"detail": "ledger busy, try again"}, status_code=503)
    finally:
        db.close()
    return {"shadow_enabled": enabled, "source": source}


CONTROL_HEADER = "x-fastlane-control"   # custom header: a cross-site page cannot send it without a CORS preflight,
                                        # and there is no CORS middleware, so the preflight fails
MAX_CONTROL_BODY = 256


@app.post("/settings/shadow")
async def set_shadow(request: Request):
    """The API's only write. Toggles shadow mode at runtime (the engine re-reads within 2 s). Paper only: this
    switches a paper-trade experiment on and off; it cannot place, size or route anything."""
    if request.headers.get(CONTROL_HEADER) != "1":
        return JSONResponse({"detail": "missing X-Fastlane-Control: 1"}, status_code=403)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_CONTROL_BODY:   # reject before reading the body
        return JSONResponse({"detail": "body too large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_CONTROL_BODY:
        return JSONResponse({"detail": "body too large"}, status_code=413)
    try:
        data = json.loads(body)
    except ValueError:
        return JSONResponse({"detail": "body is not JSON"}, status_code=400)
    if not (isinstance(data, dict) and set(data) == {"enabled"} and isinstance(data["enabled"], bool)):
        return JSONResponse({"detail": 'body must be exactly {"enabled": true|false}'}, status_code=400)
    if not DB_PATH.exists():
        return JSONResponse({"detail": "no ledger yet: start the engine once"}, status_code=503)
    db = sqlite3.connect(str(DB_PATH), timeout=2.0, isolation_level=None)   # busy_timeout 2 s: the engine writes in WAL
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(SETTINGS_DDL)
        db.execute("INSERT OR REPLACE INTO settings (key, value, updated_ts) VALUES ('shadow_enabled', ?, ?)",
                   ("1" if data["enabled"] else "0", time.time()))
        db.execute("COMMIT")
    except sqlite3.OperationalError as exc:        # locked past the timeout
        return JSONResponse({"detail": f"ledger busy: {exc}"}, status_code=503)
    finally:
        db.close()
    return {"shadow_enabled": data["enabled"], "source": "ledger"}
