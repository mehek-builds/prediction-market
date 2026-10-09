"""Ledger backups, restores and a restore drill.

    python3 -m fastlane.backup                 # back up now
    python3 -m fastlane.backup --list          # list backups, newest first
    python3 -m fastlane.backup --verify        # restore drill: restore the newest backup to a temp file and check it
    python3 -m fastlane.backup --restore PATH --yes   # replace the ledger with a backup (stop the engine first)

Backups use SQLite's online backup API, so they are consistent while the engine is writing. Each one is gzipped and
has a manifest with per-table row counts, which the restore drill compares against. The engine also backs up every
BACKUP_EVERY_HOURS (default 6) and on a clean shutdown. BACKUP_DIR (default fastlane/results/backups) can point at a
synced folder (iCloud, Dropbox, a mounted volume) so a backup survives the machine. BACKUP_KEEP (default 28) old
backups are kept.
"""
import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from fastlane.config import RESULTS_DIR, load_env
from fastlane.ledger import DB_PATH

REQUIRED_TABLES = ("events", "decisions", "trades", "marks")


def backup_dir() -> Path:
    raw = os.environ.get("BACKUP_DIR", "").strip()
    return Path(raw).expanduser() if raw else RESULTS_DIR / "backups"


def keep_count() -> int:
    try:
        return max(1, int(os.environ.get("BACKUP_KEEP", "28")))
    except ValueError:
        return 28


def table_counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _snapshot(src: Path, dest: Path) -> dict[str, int]:
    """Consistent copy of a live WAL database into a standalone (non-WAL) file. Returns its row counts."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup failed integrity check: {ok}")
        return table_counts(d)
    finally:
        s.close()
        d.close()


def snapshot(dest: Path, src: Path = DB_PATH) -> dict[str, int]:
    """A plain, uncompressed snapshot (the Vercel deploy uses this)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    counts = _snapshot(src, tmp)
    os.replace(tmp, dest)
    return counts


def backup(src: Path = DB_PATH, out_dir: Path | None = None, keep: int | None = None) -> Path:
    """Write <out_dir>/ledger-YYYYmmdd-HHMMSS.db.gz plus a .json manifest, prune old ones. Returns the backup path."""
    if not src.exists():
        raise FileNotFoundError(f"no ledger at {src}")
    out_dir = out_dir or backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    final = out_dir / f"ledger-{stamp}.db.gz"
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "ledger.db"
        counts = _snapshot(src, raw)
        part = final.with_suffix(".gz.part")
        with open(raw, "rb") as fi, gzip.open(part, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
        os.replace(part, final)
    manifest = {"created_ts": time.time(), "source": src.name, "counts": counts, "bytes": final.stat().st_size}
    final.with_suffix("").with_suffix(".json").write_text(json.dumps(manifest, indent=1))
    prune(out_dir, keep if keep is not None else keep_count())
    return final


def list_backups(out_dir: Path | None = None) -> list[Path]:
    return sorted((out_dir or backup_dir()).glob("ledger-*.db.gz"), reverse=True)


def manifest_of(path: Path) -> dict:
    m = path.with_suffix("").with_suffix(".json")
    return json.loads(m.read_text()) if m.exists() else {}


def prune(out_dir: Path, keep: int):
    for old in list_backups(out_dir)[keep:]:
        old.unlink(missing_ok=True)
        old.with_suffix("").with_suffix(".json").unlink(missing_ok=True)


def _decompress(path: Path, dest: Path):
    with gzip.open(path, "rb") as fi, open(dest, "wb") as fo:
        shutil.copyfileobj(fi, fo)


def check(db_path: Path, expect: dict[str, int] | None = None) -> dict:
    """Open a restored ledger the way the app does and prove it is usable."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        counts = table_counts(db)
        missing = [t for t in REQUIRED_TABLES if t not in counts]
        # The dashboard query: if this runs, the restored file serves the dashboard.
        if not missing:
            db.execute("SELECT t.id FROM trades t JOIN events e ON e.id = t.event_id LIMIT 1").fetchall()
    finally:
        db.close()
    mismatched = {t: (n, counts.get(t)) for t, n in (expect or {}).items() if counts.get(t) != n}
    return {"ok": ok == "ok" and not missing and not mismatched, "integrity": ok, "missing_tables": missing,
            "count_mismatch": mismatched, "counts": counts}


def verify(path: Path | None = None) -> dict:
    """Restore drill: decompress a backup (newest by default) to a temp file and check it against its manifest."""
    path = path or next(iter(list_backups()), None)
    if path is None:
        return {"ok": False, "error": "no backups found", "dir": str(backup_dir())}
    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "ledger.db"
        _decompress(path, restored)
        res = check(restored, manifest_of(path).get("counts"))
    res["backup"] = str(path)
    return res


def engine_has_ledger_open(db_path: Path) -> bool:
    """True if another process holds a write lock right now (best effort: an idle engine holds none)."""
    if not db_path.exists():
        return False
    db = sqlite3.connect(db_path, timeout=0.2)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        db.close()


def restore(path: Path, db_path: Path = DB_PATH) -> Path | None:
    """Replace the ledger with a backup. The current ledger is kept as ledger.db.pre-restore-<stamp>."""
    if engine_has_ledger_open(db_path):
        raise RuntimeError("the ledger is locked: stop the engine (python3 -m fastlane.run) first")
    with tempfile.TemporaryDirectory(dir=db_path.parent) as td:
        staged = Path(td) / "ledger.db"
        if path.suffix == ".gz":
            _decompress(path, staged)
        else:
            shutil.copyfile(path, staged)
        res = check(staged, manifest_of(path).get("counts") if path.suffix == ".gz" else None)
        if not res["ok"]:
            raise RuntimeError(f"backup did not pass the check, ledger left untouched: {res}")
        kept = None
        if db_path.exists():
            kept = db_path.with_name(f"{db_path.name}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
            snapshot(kept, db_path)  # folds the WAL in, so the kept copy is complete
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        os.replace(staged, db_path)
    return kept


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(description="Back up, verify and restore the fastlane ledger.")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", nargs="?", const="", metavar="BACKUP")
    ap.add_argument("--restore", metavar="BACKUP")
    ap.add_argument("--yes", action="store_true", help="confirm --restore")
    a = ap.parse_args(argv)
    if a.list:
        for p in list_backups():
            m = manifest_of(p)
            print(f"{p}  {p.stat().st_size / 1e6:6.2f} MB  trades={m.get('counts', {}).get('trades', '?')}")
        return 0
    if a.verify is not None:
        res = verify(Path(a.verify) if a.verify else None)
        print(json.dumps({k: v for k, v in res.items() if k != "counts"}, indent=1))
        print("RESTORE DRILL PASSED" if res["ok"] else "RESTORE DRILL FAILED")
        return 0 if res["ok"] else 1
    if a.restore:
        if not a.yes:
            print("This replaces fastlane/results/ledger.db. Stop the engine, then re-run with --yes.")
            return 2
        kept = restore(Path(a.restore))
        print(f"restored {a.restore}" + (f"; previous ledger kept at {kept}" if kept else ""))
        return 0
    path = backup()
    print(f"backup written: {path} ({path.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
