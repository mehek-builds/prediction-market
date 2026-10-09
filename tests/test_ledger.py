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
