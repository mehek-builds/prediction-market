"""Backups are consistent, verified against their manifest, restorable, and pruned."""
import gzip
import sqlite3
import time

import pytest

from fastlane import backup
from fastlane.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")   # WAL mode, like the real one
    now = time.time()
    led.event({"id": "e1", "source": "s", "headline": "h", "seen_ts": now})
    led.decision("e1", action="BUY_YES", reason="signal_yes", market_id="M1")
    led.trade(event_id="e1", opened_ts=now, venue="kalshi", market_id="M1", market_question="q", side="yes",
              contracts=1, avg_price=.5, cost=.5, fee=.02, best_ask=.5, synthetic=0)
    return led


def test_backup_writes_gzip_and_manifest(ledger, tmp_path):
    out = backup.backup(ledger.path, tmp_path / "b", keep=5)
    assert out.name.startswith("ledger-") and out.name.endswith(".db.gz")
    assert gzip.open(out).read(16).startswith(b"SQLite format 3")
    m = backup.manifest_of(out)
    assert m["counts"]["trades"] == 1 and m["counts"]["events"] == 1


def test_restore_drill_passes(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "b"))
    backup.backup(ledger.path)
    res = backup.verify()
    assert res["ok"] and res["counts"]["trades"] == 1


def test_restore_drill_catches_a_bad_backup(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "b"))
    out = backup.backup(ledger.path)
    m = backup.manifest_of(out)
    m["counts"]["trades"] = 99
    out.with_suffix("").with_suffix(".json").write_text(__import__("json").dumps(m))
    res = backup.verify()
    assert not res["ok"] and "trades" in res["count_mismatch"]
    assert backup.main(["--verify"]) == 1


def test_verify_with_no_backups(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "empty"))
    assert not backup.verify()["ok"]


def test_restore_replaces_and_keeps_previous(ledger, tmp_path):
    out = backup.backup(ledger.path, tmp_path / "b")
    ledger.trade(event_id="e2", opened_ts=time.time(), venue="kalshi", market_id="M2", market_question="q",
                 side="no", contracts=1, avg_price=.5, cost=.5, fee=.02, best_ask=.5, synthetic=0)
    ledger.db.close()
    kept = backup.restore(out, ledger.path)
    db = sqlite3.connect(ledger.path)
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1          # back to the backup
    assert sqlite3.connect(kept).execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2   # nothing lost


def test_restore_refuses_while_engine_writes(ledger, tmp_path):
    out = backup.backup(ledger.path, tmp_path / "b")
    ledger.db.execute("BEGIN IMMEDIATE")   # an engine mid-write holds this lock
    with pytest.raises(RuntimeError, match="stop the engine"):
        backup.restore(out, ledger.path)
    ledger.db.execute("ROLLBACK")


def test_restore_refuses_a_corrupt_backup(ledger, tmp_path):
    bad = tmp_path / "ledger-20990101-000000.db.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"not a database")
    ledger.db.close()
    with pytest.raises(Exception):
        backup.restore(bad, ledger.path)
    assert sqlite3.connect(ledger.path).execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_prune_keeps_newest(ledger, tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    for i in range(5):
        (d / f"ledger-2026010{i}-000000.db.gz").write_bytes(b"x")
        (d / f"ledger-2026010{i}-000000.json").write_text("{}")
    backup.prune(d, 2)
    assert [p.name for p in backup.list_backups(d)] == ["ledger-20260104-000000.db.gz", "ledger-20260103-000000.db.gz"]
    assert len(list(d.glob("*.json"))) == 2


def test_restore_carries_over_real_orders_so_caps_do_not_reset(ledger, tmp_path):
    out = backup.backup(ledger.path, tmp_path / "b")          # older backup, no real orders in it
    ledger.live_order(event_id="e9", ts=time.time(), market_id="REAL-1", side="yes", contracts=5, limit_price=.5,
                      client_order_id="c9", status="filled", fill_count=5, avg_price=.5, cost=2.5, fee=.1)
    before = ledger.live_spent_today()
    assert before > 2.5 and ledger.live_markets() == {"REAL-1"}
    ledger.db.close()
    backup.restore(out, ledger.path)
    after = Ledger(ledger.path)
    assert after.live_spent_today() == pytest.approx(before) and after.live_markets() == {"REAL-1"}
    assert after.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    # restoring again never duplicates the rows
    after.db.close()
    backup.restore(out, ledger.path)
    assert Ledger(ledger.path).db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 1


def test_restore_refuses_while_the_engine_heartbeat_is_fresh(ledger, tmp_path):
    from fastlane import live
    out = backup.backup(ledger.path, tmp_path / "b")
    ledger.db.close()
    live._write_json(live.ENGINE_FILE, {"session": "s", "heartbeat_ts": time.time()})
    with pytest.raises(RuntimeError, match="engine is running"):
        backup.restore(out, ledger.path)
    assert sqlite3.connect(ledger.path).execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_sqlite_uri_with_special_characters_in_the_path(tmp_path):
    d = tmp_path / "we?ird#dir%20x"
    d.mkdir()
    led = Ledger(d / "ledger.db")
    led.event({"id": "e1", "source": "s", "headline": "h", "seen_ts": time.time()})
    assert backup.backup(led.path, d / "b").exists()


def test_restore_prefers_the_current_ledgers_row_for_the_same_order(ledger, tmp_path):
    ledger.live_order(event_id="e9", ts=time.time(), market_id="REAL-1", side="yes", contracts=5, limit_price=.5,
                      client_order_id="c9", status="sending")
    out = backup.backup(ledger.path, tmp_path / "b")          # backup caught the order mid-flight
    ledger.live_order_update("c9", status="filled", fill_count=3, avg_price=.5, cost=1.5, fee=.1)
    ledger.db.commit()
    ledger.db.close()
    backup.restore(out, ledger.path)
    rows = sqlite3.connect(ledger.path).execute("SELECT status, fill_count FROM live_orders WHERE client_order_id='c9'").fetchall()
    assert rows == [("filled", 3)]      # the newer state wins, one row


def test_carry_count_ignores_null_client_order_ids_in_the_backup(ledger, tmp_path):
    out = backup.backup(ledger.path, tmp_path / "b")
    staged = tmp_path / "staged.db"
    staged.write_bytes(gzip.decompress(out.read_bytes()) if out.suffix == ".gz" else out.read_bytes())
    db = sqlite3.connect(staged)
    db.execute("INSERT INTO live_orders (event_id, ts, market_id, client_order_id) VALUES ('x', 1, 'M', NULL)")
    db.commit(); db.close()
    ledger.live_order(event_id="e9", ts=time.time(), market_id="REAL-1", side="yes", contracts=1, limit_price=.5,
                      client_order_id="c9", status="filled", fill_count=1, avg_price=.5, cost=.5, fee=.02)
    ledger.db.commit()
    assert backup.carry_live_orders(staged, ledger.path) == 1   # NOT IN would have returned 0 here


def test_demo_backups_are_kept_apart_from_production(ledger, tmp_path, monkeypatch):
    base = tmp_path / "bk"
    monkeypatch.setenv("BACKUP_DIR", str(base))
    monkeypatch.delenv("KALSHI_BASE_URL", raising=False)
    assert backup.backup_dir() == base
    prod = [backup.backup(tmp_path / "ledger.db", keep=5)]
    prod_files = sorted(base.glob("ledger-*"))
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    assert backup.backup_dir() == base / "demo"
    demo = backup.backup(tmp_path / "ledger.db", keep=1)
    assert demo.parent == base / "demo"
    time.sleep(1.1)
    backup.backup(tmp_path / "ledger.db", keep=1)          # prune in demo keeps 1 demo file
    assert len(list((base / "demo").glob("ledger-*.db.gz"))) == 1
    assert sorted(base.glob("ledger-*")) == prod_files      # production files untouched
    assert backup.verify()["backup"].startswith(str(base / "demo"))
    assert all(p.parent == base / "demo" for p in backup.list_backups())
