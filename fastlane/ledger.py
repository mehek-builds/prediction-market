"""SQLite ledger: every news event, every decision with per-stage timings, every paper trade and price mark."""
import json
import sqlite3
import time
from pathlib import Path

from fastlane.config import RESULTS_DIR

DB_PATH = RESULTS_DIR / "ledger.db"

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
    mid_at_decision REAL, mid_at_published REAL, mid_at_seen REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, opened_ts REAL, venue TEXT, market_id TEXT,
    market_question TEXT, side TEXT, contracts REAL, avg_price REAL, cost REAL, fee REAL,
    best_ask REAL, synthetic INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ticks (
    market_id TEXT, ts REAL, yes_bid REAL, yes_ask REAL
);
CREATE INDEX IF NOT EXISTS ticks_market_ts ON ticks (market_id, ts);
CREATE TABLE IF NOT EXISTS marks (
    event_id TEXT, horizon_s INTEGER, ts REAL, yes_ask REAL, yes_bid REAL, mid REAL,
    PRIMARY KEY (event_id, horizon_s)
);
"""


class Ledger:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(decisions)")}
        for col in ("mid_at_published", "mid_at_seen"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE decisions ADD COLUMN {col} REAL")

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

    def trade(self, **t):
        cols = list(t.keys())
        self.db.execute(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        tuple(t.values()))

    def mark(self, event_id: str, horizon_s: int, yes_ask, yes_bid, mid):
        self.db.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?,?,?,?)",
                        (event_id, horizon_s, time.time(), yes_ask, yes_bid, mid))

    def tick(self, market_id: str, ts: float, bid: float, ask: float):
        self.db.execute("INSERT INTO ticks VALUES (?,?,?,?)", (market_id, ts, bid, ask))

    def traded_markets(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT market_id FROM trades WHERE synthetic = 0")}

    def spent_today(self) -> float:
        start = time.time() - 86400
        return self.db.execute("SELECT COALESCE(SUM(cost + fee), 0) FROM trades WHERE opened_ts > ? AND synthetic = 0",
                               (start,)).fetchone()[0]
