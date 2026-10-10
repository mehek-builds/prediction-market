"""SQLite ledger: every news event, every decision with per-stage timings, every paper trade and price mark."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastlane.config import RESULTS_DIR
from fastlane.decision import shadow_settings

DB_PATH = RESULTS_DIR / "ledger.db"

SETTINGS_DDL = "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_ts REAL)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, source TEXT, headline TEXT, summary TEXT, url TEXT,
    published_ts REAL, seen_ts REAL, synthetic INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    event_id TEXT PRIMARY KEY, decided_ts REAL,
    n_candidates INTEGER, shortlist_ms REAL, jev_ms REAL, book_ms REAL, total_ms REAL,
    venue TEXT, market_id TEXT, market_question TEXT,
    -- market_conf = signal strength, p_up/p_down = YES-side/NO-side probability mass, materiality = P(decisive)
    market_conf REAL, p_up REAL, p_down REAL, materiality REAL,
    action TEXT, reason TEXT, jev_cost REAL, answers TEXT,
    mid_at_decision REAL, mid_at_published REAL, mid_at_seen REAL,
    shadow_action TEXT, shadow_reason TEXT, shadow_market_id TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, opened_ts REAL, venue TEXT, market_id TEXT,
    market_question TEXT, side TEXT, contracts REAL, avg_price REAL, cost REAL, fee REAL,
    best_ask REAL, synthetic INTEGER DEFAULT 0,
    shadow INTEGER DEFAULT 0, signal_strength REAL, signal_decisive REAL
);
CREATE TABLE IF NOT EXISTS ticks (
    market_id TEXT, ts REAL, yes_bid REAL, yes_ask REAL
);
CREATE INDEX IF NOT EXISTS ticks_market_ts ON ticks (market_id, ts);
CREATE TABLE IF NOT EXISTS marks (
    event_id TEXT, horizon_s INTEGER, ts REAL, yes_ask REAL, yes_bid REAL, mid REAL,
    PRIMARY KEY (event_id, horizon_s)
);
-- Real Kalshi orders (live.py). Empty unless the user turned real trading on. One row per attempt, written before
-- the order is sent; status: sending | filled | no_fill | duplicate | error | unknown | skipped_too_small.
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts REAL, market_id TEXT, side TEXT, contracts REAL,
    limit_price REAL, client_order_id TEXT UNIQUE, order_id TEXT, status TEXT, fill_count REAL, avg_price REAL,
    cost REAL, fee REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS x_spend (
    day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0, usd REAL DEFAULT 0, posts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feed_status (
    name TEXT PRIMARY KEY, connected INTEGER DEFAULT 0, updated_ts REAL, info TEXT
);
-- Resting paper bids (ENTRY_STYLE=post, the orders module). Paper only: nothing here ever reaches an exchange.
-- status: working | filled | partial_expired | post_expired | withdrawn
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT, event_id TEXT, venue TEXT, market_id TEXT, market_question TEXT,
    side TEXT, style TEXT, limit_price REAL, take_price REAL, requested REAL, filled REAL DEFAULT 0, avg_price REAL,
    cost REAL DEFAULT 0, fee REAL DEFAULT 0, status TEXT, note TEXT, created_ts REAL, expires_ts REAL, updated_ts REAL,
    closed_ts REAL, trade_id INTEGER, signal_strength REAL, signal_decisive REAL, synthetic INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS paper_orders_status ON paper_orders (status, market_id);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, ts REAL, contracts REAL, price REAL, evidence TEXT,
    ask_seen REAL, qty_seen REAL
);
-- Scheduled data releases (fastlane/releases.py). One row per release attempt; id = "<kind>-<period>".
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY, kind TEXT, series TEXT, period TEXT, scheduled_ts REAL, fetched_ts REAL, value REAL,
    raw TEXT, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS release_markets (
    market_id TEXT PRIMARY KEY, series TEXT, rules_primary TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL,
    yes_sub_title TEXT, template TEXT, parsed TEXT, fetched_ts REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS bls_requests (
    day TEXT PRIMARY KEY, n INTEGER DEFAULT 0
);
-- Order-book snapshots around a scheduled release (releases.py): every candidate market at decision and +5/+30/+60 s.
CREATE TABLE IF NOT EXISTS release_books (
    release_id TEXT, market_id TEXT, label TEXT, ts REAL, yes_bid REAL, yes_ask REAL, bid_qty REAL, ask_qty REAL,
    PRIMARY KEY (release_id, market_id, label)
);
""" + SETTINGS_DDL + ";\n"

# Filled rows count at no less than fill_count * limit cost: an IOC order never fills worse than its limit, so this
# bound holds whatever the response's price fields mean (YES leg or held side).
# A real order whose outcome is not known yet (or never will be) counts against the caps at its full limit cost.
LIVE_PENDING = ("sending", "unknown", "duplicate")

MIGRATIONS = [  # (table, column, type): added to ledgers created before the column existed
    ("decisions", "mid_at_published", "REAL"), ("decisions", "mid_at_seen", "REAL"),
    ("decisions", "shadow_action", "TEXT"), ("decisions", "shadow_reason", "TEXT"),
    ("decisions", "shadow_market_id", "TEXT"),
    ("trades", "shadow", "INTEGER DEFAULT 0"), ("trades", "signal_strength", "REAL"), ("trades", "signal_decisive", "REAL"),
    # 0.5.0 (additive: a 0.4.0 binary opens this ledger and ignores them)
    ("decisions", "quote_wait_ms", "REAL"), ("decisions", "n_live_quotes", "INTEGER"),
    ("decisions", "starter_action", "TEXT"), ("decisions", "starter_reason", "TEXT"),
    ("decisions", "starter_market_id", "TEXT"),
    ("trades", "book", "TEXT"), ("trades", "entry_style", "TEXT"), ("trades", "order_id", "INTEGER"),
    ("trades", "limit_price", "REAL"),
]

# Which paper book a trade belongs to. Rows written before 0.5.0 have no `book` until the backfill below runs; the
# COALESCE keeps every query right on a ledger that was opened read-only and not migrated yet.
BOOK_SQL = "COALESCE(book, CASE WHEN shadow = 1 THEN 'shadow' ELSE 'live' END)"
BOOKS = ("live", "shadow", "starter")

# Marks are keyed by event id. The real path records horizon-0 and scheduled marks under the event id for EVERY chosen
# market, including PASS decisions (calibration). The shadow rule can pick a different market on the same event, so a
# shadow trade gets its own key. Event ids are 16-hex feed hashes, `move-<ticker>-<ts>` or `syn-<hash>`, so the prefix
# cannot collide with a real id.
SHADOW_MARK_PREFIX = "shadow:"
STARTER_MARK_PREFIX = "starter:"


INDEX_MIGRATIONS = [  # idempotent; the dashboard API looks marks up by event_id several times per decision row
    "CREATE INDEX IF NOT EXISTS marks_event ON marks (event_id, horizon_s)",
    "CREATE INDEX IF NOT EXISTS ticks_ts ON ticks (ts)",  # /status reads MAX(ts) and a 1h count over ticks
]


def utc_day(ts: float | None = None) -> str:
    """'YYYY-MM-DD' of ts (default now) in UTC. The X budget is per UTC day."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%d")


def mark_key(event_id: str, book) -> str:
    """Marks key per book: live -> event id, shadow -> "shadow:<id>", starter -> "starter:<id>". A bool still means
    shadow (True) or live (False) for callers from before 0.5.0."""
    if book in (True, 1, "shadow"):
        return f"{SHADOW_MARK_PREFIX}{event_id}"
    if book == "starter":
        return f"{STARTER_MARK_PREFIX}{event_id}"
    return event_id


def shadow_enabled_from(db: sqlite3.Connection) -> tuple[bool, str]:
    """(enabled, source). The ledger row wins; without one, env SHADOW_ENABLED (default true) via shadow_settings().

    Tolerates a ledger without the settings table (old file not yet opened by the engine): falls back to env."""
    try:
        row = db.execute("SELECT value FROM settings WHERE key = 'shadow_enabled'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise   # transient (locked, busy): the caller keeps its previous value
        row = None
    if row:
        return row[0] == "1", "ledger"
    return shadow_settings()[0], "env"


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


class Ledger:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            if col not in columns(self.db, table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.execute("UPDATE trades SET book = CASE WHEN shadow = 1 THEN 'shadow' ELSE 'live' END WHERE book IS NULL")
        for stmt in INDEX_MIGRATIONS:
            self.db.execute(stmt)

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO settings (key, value, updated_ts) VALUES (?, ?, ?)",
                        (key, value, time.time()))

    def event(self, e: dict):
        self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)",
            (e["id"], e["source"], e["headline"], e.get("summary", ""), e.get("url", ""),
             e.get("published_ts"), e["seen_ts"], int(e.get("synthetic", False))))

    def decision(self, event_id: str, **d):
        d.setdefault("decided_ts", time.time())
        if isinstance(d.get("answers"), dict):
            d["answers"] = json.dumps(d["answers"])
        cols = ["event_id", *d.keys()]
        self.db.execute(f"INSERT OR REPLACE INTO decisions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        (event_id, *d.values()))

    def trade(self, **t) -> int:
        t.setdefault("book", "shadow" if t.get("shadow") else "live")
        cols = list(t.keys())
        return self.db.execute(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               tuple(t.values())).lastrowid

    def trade_update(self, trade_id: int, **t):
        sets = ", ".join(f"{k} = ?" for k in t)
        self.db.execute(f"UPDATE trades SET {sets} WHERE id = ?", (*t.values(), trade_id))

    def mark(self, event_id: str, horizon_s: int, yes_ask, yes_bid, mid):
        self.db.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?,?,?,?)",
                        (event_id, horizon_s, time.time(), yes_ask, yes_bid, mid))

    def tick(self, market_id: str, ts: float, bid: float, ask: float):
        self.db.execute("INSERT INTO ticks VALUES (?,?,?,?)", (market_id, ts, bid, ask))

    def shadow_decision(self, event_id: str, action: str | None, reason: str | None, market_id: str | None = None):
        self.db.execute("UPDATE decisions SET shadow_action = ?, shadow_reason = ?, shadow_market_id = ? "
                        "WHERE event_id = ?", (action, reason, market_id, event_id))

    def starter_decision(self, event_id: str, action: str | None, reason: str | None, market_id: str | None = None):
        self.db.execute("UPDATE decisions SET starter_action = ?, starter_reason = ?, starter_market_id = ? "
                        "WHERE event_id = ?", (action, reason, market_id, event_id))

    def traded_markets(self, book: str = "live", shadow: bool | None = None) -> set[str]:
        """Markets with a real-paper position in `book` (non-synthetic), plus markets with a working resting order in it.
        `shadow=True/False` is the pre-0.5.0 spelling of book shadow / live."""
        if shadow is not None:
            book = "shadow" if shadow else "live"
        held = {r[0] for r in self.db.execute(
            f"SELECT market_id FROM trades WHERE synthetic = 0 AND {BOOK_SQL} = ?", (book,))}
        return held | self.open_order_markets(book)

    def spent_today(self) -> float:
        start = time.time() - 86400
        spent = self.db.execute(f"SELECT COALESCE(SUM(cost + fee), 0) FROM trades WHERE opened_ts > ? AND synthetic = 0 "
                                f"AND {BOOK_SQL} = 'live'", (start,)).fetchone()[0]
        return spent + self.working_live_exposure()

    def working_live_exposure(self) -> float:
        """Dollars still committed by working LIVE-book resting orders (unfilled part at the limit); the filled part is
        already in trades. Counted against the bankroll so ENTRY_STYLE_LIVE=post cannot place orders past it."""
        try:
            return self.db.execute("SELECT COALESCE(SUM((requested - filled) * limit_price), 0) FROM paper_orders "
                                   "WHERE status = 'working' AND book = 'live'").fetchone()[0]
        except Exception:      # an unmigrated read-only ledger has no paper_orders table
            return 0.0

    # ---------- resting paper orders (orders.py) ----------
    def order_place(self, **o) -> int:
        cols = list(o.keys())
        return self.db.execute(f"INSERT INTO paper_orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               tuple(o.values())).lastrowid

    def order_update(self, order_id: int, **o):
        sets = ", ".join(f"{k} = ?" for k in o)
        self.db.execute(f"UPDATE paper_orders SET {sets} WHERE id = ?", (*o.values(), order_id))

    def _orders(self, where: str, args: tuple = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM paper_orders {where}", args)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def order_get(self, order_id: int) -> dict | None:
        rows = self._orders("WHERE id = ?", (order_id,))
        return rows[0] if rows else None

    def orders_working(self) -> list[dict]:
        return self._orders("WHERE status = 'working' ORDER BY created_ts")

    def orders_recent(self, limit: int = 50, since_ts: float = 0.0) -> list[dict]:
        """Orders that are no longer working, closed at or after `since_ts`, newest first."""
        return self._orders("WHERE status != 'working' AND COALESCE(closed_ts, updated_ts, created_ts) >= ? "
                            "ORDER BY COALESCE(closed_ts, updated_ts, created_ts) DESC LIMIT ?", (since_ts, limit))

    def fill_add(self, **f) -> int:
        cols = list(f.keys())
        return self.db.execute(f"INSERT INTO paper_fills ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               tuple(f.values())).lastrowid

    def open_order_markets(self, book: str) -> set[str]:
        return {r[0] for r in self.db.execute(
            "SELECT market_id FROM paper_orders WHERE status = 'working' AND book = ? AND synthetic = 0", (book,))}

    # ---------- scheduled releases (releases.py) ----------
    def bls_request_add(self, day: str) -> int:
        """Count one BLS request for `day` BEFORE it is sent. Returns the new total."""
        self.db.execute("INSERT INTO bls_requests (day, n) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET n = n + 1", (day,))
        return self.bls_requests(day)

    def bls_requests(self, day: str) -> int:
        row = self.db.execute("SELECT n FROM bls_requests WHERE day = ?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def release_get(self, release_id: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM releases WHERE id = ?", (release_id,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def release_put(self, **r):
        cols = list(r.keys())
        self.db.execute(f"INSERT OR REPLACE INTO releases ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(r.values()))

    def release_market_put(self, **m):
        cols = list(m.keys())
        self.db.execute(f"INSERT OR REPLACE INTO release_markets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(m.values()))

    def release_book_put(self, **r):
        cols = list(r.keys())
        self.db.execute(f"INSERT OR REPLACE INTO release_books ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(r.values()))

    def release_books(self, release_id: str) -> list[dict]:
        cur = self.db.execute("SELECT * FROM release_books WHERE release_id = ? ORDER BY ts, market_id", (release_id,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def x_spend_add(self, day: str, usd: float, calls: int = 1, posts: int = 0) -> None:
        self.db.execute("INSERT INTO x_spend VALUES (?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                        "calls = calls + excluded.calls, usd = usd + excluded.usd, posts = posts + excluded.posts",
                        (day, calls, usd, posts))

    def x_spend(self, day: str) -> tuple[int, float]:
        """(calls, usd) for the day, (0, 0.0) when absent."""
        row = self.db.execute("SELECT calls, usd FROM x_spend WHERE day = ?", (day,)).fetchone()
        return (int(row[0]), float(row[1])) if row else (0, 0.0)

    def feed_status_set(self, name: str, connected: bool, info: dict | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO feed_status VALUES (?,?,?,?)",
                        (name, int(bool(connected)), time.time(), json.dumps(info or {})))

    def feed_status(self, name: str) -> dict | None:
        """{"connected": bool, "updated_ts": float, "info": dict} or None."""
        row = self.db.execute("SELECT connected, updated_ts, info FROM feed_status WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        try:
            info = json.loads(row[2] or "{}")
        except ValueError:
            info = {}
        return {"connected": bool(row[0]), "updated_ts": row[1], "info": info}

    # ---------- real orders (live.py) ----------
    def live_order(self, **o):
        cols = list(o.keys())
        self.db.execute(f"INSERT OR IGNORE INTO live_orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(o.values()))

    def live_order_get(self, client_order_id: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM live_orders WHERE client_order_id = ?", (client_order_id,))
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def live_order_update(self, client_order_id: str, **o):
        sets = ", ".join(f"{k} = ?" for k in o)
        self.db.execute(f"UPDATE live_orders SET {sets} WHERE client_order_id = ?", (*o.values(), client_order_id))

    def live_markets(self) -> set[str]:
        """Markets with any real order that may hold a position (failed-before-sending rows excluded)."""
        return {r[0] for r in self.db.execute(
            "SELECT market_id FROM live_orders WHERE status IN ('filled', 'duplicate', 'sending', 'unknown')")}

    def live_orders_last_hour(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self.db.execute("SELECT COUNT(*) FROM live_orders WHERE ts > ? AND status != 'skipped_too_small'",
                               (now - 3600,)).fetchone()[0]

    def live_spent_today(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        q = f"""SELECT COALESCE(SUM(CASE WHEN status IN ({",".join("?" * len(LIVE_PENDING))})
                        THEN contracts * (limit_price + 0.07 * limit_price * (1 - limit_price))
                        ELSE MAX(COALESCE(cost, 0) + COALESCE(fee, 0),
                                 COALESCE(fill_count, 0) * (limit_price + 0.07 * limit_price * (1 - limit_price)))
                        END), 0)
                FROM live_orders WHERE ts > ?"""
        return self.db.execute(q, (*LIVE_PENDING, now - 86400)).fetchone()[0]
