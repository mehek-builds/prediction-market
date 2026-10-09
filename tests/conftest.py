import base64
import os
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from fastlane import books
from fastlane.ledger import Ledger
from fastlane.universe import Universe

_ENV_VARS = ["OPENROUTER_API_KEY", "JEV_MODEL", "SEC_USER_AGENT", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH",
             "SHADOW_ENABLED", "SHADOW_SIGNAL_THRESHOLD", "SHADOW_DECISIVE_MIN", "MAX_SPREAD_CENTS", "COST_TO_ROOM_MAX",
             "XAI_API_KEY", "XAI_X_HANDLES", "XAI_POLL_SECONDS", "XAI_DAILY_BUDGET_USD", "XAI_WINDOW_DAYS",
             "XAI_WINDOW_HOURS", "XAI_WINDOW_TZ", "BSKY_ENABLED", "BSKY_HANDLES", "MIN_ENTRY_PRICE", "MOVE_DENY_RE",
             "LIVE_TRADING_ENABLED", "LIVE_MAX_ORDER_USD", "LIVE_MAX_DAILY_USD", "LIVE_MAX_ORDERS_PER_HOUR", "LIVE_ALLOW_NO_SIDE", "LIVE_ALLOW_REMOTE_CONTROL", "KALSHI_BASE_URL",
             "JEV_MAX_CALLS_PER_HOUR", "JEV_MAX_USD_PER_DAY", "API_RATE_LIMIT_PER_MIN", "API_RATE_LIMIT_BURST",
             "BACKUP_DIR", "BACKUP_KEEP", "BACKUP_EVERY_HOURS", "FASTLANE_SENTRY_DSN", "FASTLANE_TELEMETRY",
             "FASTLANE_HOSTED", "FASTLANE_DASHBOARD_PASSWORD_HASH", "VERCEL"]

OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, source TEXT, headline TEXT, summary TEXT, url TEXT,
    published_ts REAL, seen_ts REAL, synthetic INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    event_id TEXT PRIMARY KEY, decided_ts REAL,
    n_candidates INTEGER, shortlist_ms REAL, jev_ms REAL, book_ms REAL, total_ms REAL,
    venue TEXT, market_id TEXT, market_question TEXT,
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
"""  # the v0.1.1 ledger schema, kept verbatim to test migration and read-only API tolerance


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Host env must never leak into tests, and tests never write into the real results directory."""
    from fastlane import api, backup, errors, live
    api.app.middleware_stack = None  # rebuilt on the next request: fresh rate-limit buckets per test
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FASTLANE_TELEMETRY", "0")
    monkeypatch.setattr(live, "MODE_FILE", tmp_path / "trading_mode.json")
    monkeypatch.setattr(live, "ENGINE_FILE", tmp_path / "engine_state.json")
    monkeypatch.setattr(backup, "DB_PATH", tmp_path / "no-such-ledger.db")
    monkeypatch.setattr(errors, "ERROR_LOG", tmp_path / "errors.log")


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="session")
def ed25519_pem() -> str:
    return _pem(ed25519.Ed25519PrivateKey.generate())


def pem_body(pem: str) -> str:
    return "".join(l for l in pem.splitlines() if not l.startswith("-----"))


@pytest.fixture
def tiny_universe() -> Universe:
    """A handful of real-looking markets padded with unique-token fillers so IDF of a rare word exceeds min_score 6."""
    mk = lambda venue, id, q, cat, ask, bid, vol, **kw: dict(  # noqa: E731
        venue=venue, id=id, question=q, category=cat, yes_ask=ask, yes_bid=bid, volume_24h=vol, **kw)
    markets = [
        mk("kalshi", "CPI-A", "CPI inflation report: strike 3.0", "Economics", .70, .68, 100),
        mk("kalshi", "CPI-B", "CPI inflation report: strike 3.5", "Economics", .50, .48, 900),
        mk("kalshi", "CPI-C", "CPI inflation report: strike 4.0", "Economics", .20, .18, 500),
        mk("kalshi", "AVNT-1", "Avient quarterly earnings beat", "Companies", .55, .53, 300),
        mk("polymarket", "9001", "Bitcoin ladder moonshot", "Crypto", .30, .28, 50,
           yes_token="tokY", no_token="tokN"),
        mk("kalshi", "BTC-1", "Bitcoin closes higher", "Crypto", .45, .43, 70),
        mk("kalshi", "GEN-1", "Senate passes budget bill", "Politics", .40, .38, 10),
        mk("kalshi", "GEN-2", "Rainfall exceeds average", "Weather", .60, .58, 10),
    ]
    markets += [mk("kalshi", f"F-{i}", f"filler topic fill{i}x", "Other", .5, .48, 1) for i in range(500)]
    u = Universe()
    u._set(markets)
    return u


@pytest.fixture
def book_factory():
    def make(venue="kalshi", yes_asks=(), no_asks=()):
        return books.Book(venue, "M", list(yes_asks), list(no_asks))
    return make


@pytest.fixture
def tmp_ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


@pytest.fixture
def old_ledger_path(tmp_path):
    """A ledger file in the v0.1.1 schema with one live event, decision, trade and mark."""
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO events VALUES ('e1','cnbc','Old news','','http://x',900.0,905.0,0)")
    db.execute("INSERT INTO decisions (event_id, decided_ts, action, reason, venue, market_id, market_question, "
               "market_conf, p_up, p_down, materiality, mid_at_decision) "
               "VALUES ('e1',906.0,'BUY_YES','signal_yes','kalshi','MK1','Q1?',.9,.9,.05,.6,.40)")
    db.execute("INSERT INTO trades (event_id, opened_ts, venue, market_id, market_question, side, contracts, "
               "avg_price, cost, fee, best_ask, synthetic) VALUES ('e1',906.0,'kalshi','MK1','Q1?','yes',10,.4,4.0,.05,.4,0)")
    db.execute("INSERT INTO marks VALUES ('e1',5,911.0,.47,.45,.46)")
    db.commit(); db.close()
    return path
