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
