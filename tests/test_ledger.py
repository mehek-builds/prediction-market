import time

import pytest

from fastlane.ledger import Ledger, columns, mark_key, utc_day


def test_old_ledger_migrates_on_open(old_ledger_path):
    L = Ledger(old_ledger_path)
    assert {"shadow", "signal_strength", "signal_decisive"} <= columns(L.db, "trades")
    assert {"shadow_action", "shadow_reason", "shadow_market_id", "mid_at_published", "mid_at_seen"} <= columns(L.db, "decisions")
    assert L.db.execute("SELECT shadow, signal_strength FROM trades").fetchall() == [(0, None)]   # old rows are real trades
    assert L.traded_markets() == {"MK1"} and L.traded_markets(shadow=True) == set()
    Ledger(old_ledger_path)  # second open is a no-op (no duplicate-column error)


def test_shadow_rows_are_separate(tmp_ledger):
    L = tmp_ledger
    L.trade(event_id="e1", opened_ts=1e12, venue="kalshi", market_id="MK1", market_question="Q", side="yes",
            contracts=10, avg_price=.4, cost=4.0, fee=.05, best_ask=.4, synthetic=0, shadow=1, signal_strength=.72)
    assert L.traded_markets() == set() and L.traded_markets(shadow=True) == {"MK1"}
    assert L.spent_today() == 0
    L.decision("e1", action="PASS", reason="weak_signal")
    L.shadow_decision("e1", "BUY_YES", "signal_yes", "MK1")
    assert L.db.execute("SELECT shadow_action, shadow_reason, shadow_market_id FROM decisions").fetchone() == ("BUY_YES", "signal_yes", "MK1")


def test_mark_key():
    assert mark_key("abc", False) == "abc" and mark_key("abc", True) == "shadow:abc"


def test_fresh_ledger_has_new_columns(tmp_ledger):
    assert {"shadow", "signal_strength", "signal_decisive"} <= columns(tmp_ledger.db, "trades")


def test_marks_event_index_created_and_idempotent(tmp_path, old_ledger_path):
    from fastlane.ledger import Ledger
    for path in (tmp_path / "fresh.db", old_ledger_path, old_ledger_path):   # fresh, migrated old, re-opened
        led = Ledger(path)
        idx = [r[1] for r in led.db.execute("PRAGMA index_list(marks)")]
        assert "marks_event" in idx


def test_new_tables_on_fresh_and_old_ledgers(tmp_ledger, old_ledger_path):
    for L in (tmp_ledger, Ledger(old_ledger_path)):
        assert {"day", "calls", "usd", "posts"} == columns(L.db, "x_spend")
        assert {"name", "connected", "updated_ts", "info"} == columns(L.db, "feed_status")


def test_x_spend_accumulates_and_persists(tmp_path):
    L = Ledger(tmp_path / "l.db")
    L.x_spend_add("2026-10-09", 0.055); L.x_spend_add("2026-10-09", 0.06, posts=3); L.x_spend_add("2026-10-10", 0.01)
    assert L.x_spend("2026-10-09") == (2, pytest.approx(0.115)) and L.x_spend("2026-10-11") == (0, 0.0)
    assert Ledger(tmp_path / "l.db").x_spend("2026-10-09")[0] == 2


def test_feed_status_roundtrip(tmp_ledger):
    assert tmp_ledger.feed_status("bsky") is None
    tmp_ledger.feed_status_set("bsky", True, {"mode": "ws"})
    s = tmp_ledger.feed_status("bsky")
    assert s["connected"] is True and s["info"] == {"mode": "ws"} and abs(s["updated_ts"] - time.time()) < 5
    tmp_ledger.feed_status_set("bsky", False)
    assert tmp_ledger.feed_status("bsky")["connected"] is False and tmp_ledger.feed_status("bsky")["info"] == {}


def test_utc_day():
    assert utc_day(1_700_000_000) == "2023-11-14"
    midnight = 1_699_920_000                       # 2023-11-14 00:00:00 UTC
    assert utc_day(midnight - 1) == "2023-11-13" and utc_day(midnight) == "2023-11-14"


def test_settings_table_and_precedence(tmp_ledger, monkeypatch):
    from fastlane.ledger import shadow_enabled_from
    L = tmp_ledger
    assert L.get_setting("shadow_enabled") is None
    assert shadow_enabled_from(L.db) == (True, "env")                   # no row, no env: default true
    monkeypatch.setenv("SHADOW_ENABLED", "false")
    assert shadow_enabled_from(L.db) == (False, "env")
    L.set_setting("shadow_enabled", "1")
    assert shadow_enabled_from(L.db) == (True, "ledger")                # row wins over env
    L.set_setting("shadow_enabled", "0")
    assert L.get_setting("shadow_enabled") == "0" and shadow_enabled_from(L.db) == (False, "ledger")


def test_set_setting_replaces_and_stamps(tmp_ledger):
    L = tmp_ledger
    L.set_setting("shadow_enabled", "1")
    L.set_setting("shadow_enabled", "0")
    rows = L.db.execute("SELECT key, value, updated_ts FROM settings").fetchall()
    assert len(rows) == 1 and rows[0][:2] == ("shadow_enabled", "0") and rows[0][2] == pytest.approx(time.time(), abs=5)


def test_shadow_enabled_from_tolerates_missing_table():
    import sqlite3
    from fastlane.ledger import shadow_enabled_from
    db = sqlite3.connect(":memory:")                                    # no settings table at all
    assert shadow_enabled_from(db) == (True, "env")


def test_settings_table_migrates_on_old_ledger(old_ledger_path):
    L = Ledger(old_ledger_path)
    assert "settings" in {r[0] for r in L.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    Ledger(old_ledger_path)                                              # idempotent


def _index_names(L):
    return {r[0] for r in L.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_ticks_ts_index_on_fresh_and_old_ledger_idempotent(tmp_path, old_ledger_path):
    fresh = Ledger(tmp_path / "fresh.db")
    assert "ticks_ts" in _index_names(fresh)
    Ledger(tmp_path / "fresh.db")                                        # reopen: no "already exists" error
    old = Ledger(old_ledger_path)
    assert "ticks_ts" in _index_names(old)
    assert Ledger(old_ledger_path) and "ticks_ts" in _index_names(Ledger(old_ledger_path))


def test_index_migrations_list_includes_ticks_ts():
    from fastlane.ledger import INDEX_MIGRATIONS
    assert any("ticks_ts" in s and "ON ticks (ts)" in s for s in INDEX_MIGRATIONS)
    assert all("IF NOT EXISTS" in s for s in INDEX_MIGRATIONS)


def test_shadow_enabled_from_reraises_transient_errors():
    import sqlite3
    from fastlane.ledger import shadow_enabled_from

    class Locked:
        def execute(self, *a):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        shadow_enabled_from(Locked())


# ---------- v0.5.0: additive columns and tables, book backfill ----------
NEW_TRADE_COLS = {"book", "entry_style", "order_id", "limit_price"}
NEW_DECISION_COLS = {"quote_wait_ms", "n_live_quotes", "starter_action", "starter_reason", "starter_market_id"}
NEW_TABLES = {"paper_orders", "paper_fills", "releases", "release_markets", "bls_requests"}


def _tables(L):
    return {r[0] for r in L.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_new_columns_and_tables_on_a_fresh_ledger(tmp_ledger):
    assert NEW_TRADE_COLS <= columns(tmp_ledger.db, "trades")
    assert NEW_DECISION_COLS <= columns(tmp_ledger.db, "decisions")
    assert NEW_TABLES <= _tables(tmp_ledger)
    assert {"id", "book", "event_id", "market_id", "side", "style", "limit_price", "take_price", "requested", "filled",
            "avg_price", "cost", "fee", "status", "note", "created_ts", "expires_ts", "updated_ts", "closed_ts", "trade_id",
            "signal_strength", "signal_decisive", "synthetic"} <= columns(tmp_ledger.db, "paper_orders")
    assert {"order_id", "ts", "contracts", "price", "evidence", "ask_seen", "qty_seen"} <= columns(tmp_ledger.db, "paper_fills")
    assert {"day", "n"} == columns(tmp_ledger.db, "bls_requests")


def test_new_columns_and_tables_on_an_old_ledger_and_second_open(old_ledger_path):
    L = Ledger(old_ledger_path)
    assert NEW_TRADE_COLS <= columns(L.db, "trades") and NEW_DECISION_COLS <= columns(L.db, "decisions")
    assert NEW_TABLES <= _tables(L)
    Ledger(old_ledger_path)                                   # idempotent


def test_book_backfill_from_shadow_flag(tmp_path):
    import sqlite3
    from conftest import OLD_SCHEMA
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("ALTER TABLE trades ADD COLUMN shadow INTEGER DEFAULT 0")
    for i, shadow in enumerate((0, 1, 0, 1)):
        db.execute("INSERT INTO trades (event_id, opened_ts, venue, market_id, side, contracts, avg_price, cost, fee, shadow) "
                   "VALUES (?, 1, 'kalshi', ?, 'yes', 1, .5, .5, 0, ?)", (f"e{i}", f"MK{i}", shadow))
    db.commit(); db.close()
    L = Ledger(path)
    assert L.db.execute("SELECT market_id, shadow, book FROM trades ORDER BY id").fetchall() == [
        ("MK0", 0, "live"), ("MK1", 1, "shadow"), ("MK2", 0, "live"), ("MK3", 1, "shadow")]
    L.db.execute("UPDATE trades SET book = 'starter' WHERE market_id = 'MK0'")
    assert Ledger(path).db.execute("SELECT book FROM trades WHERE market_id = 'MK0'").fetchone() == ("starter",)   # never overwritten


def test_trade_defaults_book_from_shadow_and_accepts_starter(tmp_ledger):
    base = dict(event_id="e", opened_ts=1e12, venue="kalshi", side="yes", contracts=1, avg_price=.5, cost=.5, fee=0, best_ask=.5,
                synthetic=0)
    tmp_ledger.trade(market_id="A", market_question="Q", shadow=0, **base)
    tmp_ledger.trade(market_id="B", market_question="Q", shadow=1, **base)
    tmp_ledger.trade(market_id="C", market_question="Q", shadow=0, book="starter", **base)
    assert dict(tmp_ledger.db.execute("SELECT market_id, book FROM trades")) == {"A": "live", "B": "shadow", "C": "starter"}
    assert tmp_ledger.traded_markets() == {"A"} and tmp_ledger.traded_markets("starter") == {"C"}
    assert tmp_ledger.traded_markets(shadow=True) == {"B"} and tmp_ledger.traded_markets(shadow=False) == {"A"}


def test_starter_trades_never_count_in_spent_today(tmp_ledger):
    import time as _t
    tmp_ledger.trade(event_id="e", opened_ts=_t.time(), venue="kalshi", market_id="C", market_question="Q", side="yes",
                     contracts=10, avg_price=.5, cost=5.0, fee=.1, best_ask=.5, synthetic=0, book="starter")
    tmp_ledger.trade(event_id="e2", opened_ts=_t.time(), venue="kalshi", market_id="D", market_question="Q", side="yes",
                     contracts=10, avg_price=.5, cost=7.0, fee=.2, best_ask=.5, synthetic=0, shadow=0)
    assert tmp_ledger.spent_today() == pytest.approx(7.2)


def test_mark_key_is_book_aware_and_bool_compatible():
    assert mark_key("x", "live") == "x" and mark_key("x", "shadow") == "shadow:x" and mark_key("x", "starter") == "starter:x"
    assert mark_key("x", True) == "shadow:x" and mark_key("x", False) == "x" and mark_key("x", 1) == "shadow:x"


def test_open_order_markets_only_working_nonsynthetic_in_that_book(tmp_ledger):
    o = dict(venue="kalshi", side="yes", style="post", limit_price=.5, requested=10, created_ts=1.0)
    tmp_ledger.order_place(book="shadow", market_id="W1", status="working", synthetic=0, **o)
    tmp_ledger.order_place(book="shadow", market_id="W2", status="filled", synthetic=0, **o)
    tmp_ledger.order_place(book="shadow", market_id="W3", status="working", synthetic=1, **o)
    tmp_ledger.order_place(book="starter", market_id="W4", status="working", synthetic=0, **o)
    assert tmp_ledger.open_order_markets("shadow") == {"W1"} and tmp_ledger.open_order_markets("starter") == {"W4"}
    assert tmp_ledger.open_order_markets("live") == set()
    assert tmp_ledger.traded_markets("shadow") == {"W1"}          # a working order counts as a position
    assert tmp_ledger.traded_markets("live") == set()


def test_order_update_get_recent_and_fill_add(tmp_ledger):
    oid = tmp_ledger.order_place(book="shadow", market_id="M", venue="kalshi", side="yes", style="post", limit_price=.5,
                                 requested=10, status="working", created_ts=100.0, synthetic=0)
    assert tmp_ledger.order_get(oid)["filled"] == 0 and [o["id"] for o in tmp_ledger.orders_working()] == [oid]
    tmp_ledger.order_update(oid, status="post_expired", closed_ts=500.0)
    assert tmp_ledger.orders_working() == [] and tmp_ledger.order_get(99) is None
    assert [o["id"] for o in tmp_ledger.orders_recent(10, since_ts=400.0)] == [oid]
    assert tmp_ledger.orders_recent(10, since_ts=600.0) == []
    fid = tmp_ledger.fill_add(order_id=oid, ts=1.0, contracts=3, price=.5, evidence="{}", ask_seen=.5, qty_seen=9)
    assert fid == 1 and tmp_ledger.db.execute("SELECT contracts, qty_seen FROM paper_fills").fetchone() == (3, 9)


def test_trade_update_changes_only_the_named_fields(tmp_ledger):
    tid = tmp_ledger.trade(event_id="e", opened_ts=1.0, venue="kalshi", market_id="M", market_question="Q", side="yes",
                           contracts=30, avg_price=.54, cost=16.2, fee=.3, best_ask=.55, synthetic=0, book="shadow")
    tmp_ledger.trade_update(tid, contracts=100, cost=54.0, fee=1.2)
    assert tmp_ledger.db.execute("SELECT contracts, avg_price, cost, fee, best_ask, book FROM trades WHERE id=?", (tid,)).fetchone() \
        == (100, .54, 54.0, 1.2, .55, "shadow")


def test_bls_request_add_counts_per_day_and_persists(tmp_path):
    L = Ledger(tmp_path / "l.db")
    assert L.bls_requests("2026-10-14") == 0
    assert [L.bls_request_add("2026-10-14") for _ in range(3)] == [1, 2, 3]
    L.bls_request_add("2026-10-15")
    assert L.bls_requests("2026-10-14") == 3 and L.bls_requests("2026-10-15") == 1
    assert Ledger(tmp_path / "l.db").bls_requests("2026-10-14") == 3


def test_release_and_market_rows_roundtrip(tmp_ledger):
    tmp_ledger.release_put(id="cpi-2026-09", kind="cpi", status="armed", note="")
    tmp_ledger.release_put(id="cpi-2026-09", kind="cpi", status="done", value=0.4)
    r = tmp_ledger.release_get("cpi-2026-09")
    assert r["status"] == "done" and r["value"] == 0.4 and tmp_ledger.release_get("nope") is None
    tmp_ledger.release_market_put(market_id="KXCPI-26SEP-T0.3", series="KXCPI", strike_type="greater", floor_strike=.3)
    assert tmp_ledger.db.execute("SELECT series, floor_strike FROM release_markets").fetchone() == ("KXCPI", .3)


def test_a_v040_style_reader_still_opens_a_v050_ledger(tmp_ledger):
    """Additive only: the 0.1.1 column set of every old table is still present, so an older binary keeps working."""
    import sqlite3
    from conftest import OLD_SCHEMA
    old = sqlite3.connect(":memory:")
    old.executescript(OLD_SCHEMA)
    for table in ("events", "decisions", "trades", "ticks", "marks"):
        old_cols = {r[1] for r in old.execute(f"PRAGMA table_info({table})")}
        assert old_cols <= columns(tmp_ledger.db, table), table
