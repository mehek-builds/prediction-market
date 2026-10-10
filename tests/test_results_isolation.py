"""The suite never reads or writes the real fastlane/results folder (v0.6.1)."""
import sys
from pathlib import Path

import pytest

import conftest
from fastlane import config
from fastlane.ledger import Ledger


def _under_real_results(value: Path) -> bool:
    return any(value == r or r in value.parents for r in conftest._real_results_dirs())


def test_no_fastlane_module_path_points_into_a_real_results_folder():
    bad = []
    for name, mod in list(sys.modules.items()):
        if name == "fastlane" or name.startswith("fastlane."):
            bad += [f"{name}.{a}" for a, v in vars(mod).items() if isinstance(v, Path) and _under_real_results(v)]
    assert bad == []


def test_default_paths_resolve_at_call_time_to_the_temp_folder(tmp_path):
    from fastlane import backup, deploy, live
    led = Ledger()
    assert tmp_path in led.path.parents and not _under_real_results(led.path)
    assert deploy.ledger_fingerprint() == "" or tmp_path in deploy.DB_PATH.parents
    assert live.read_mode() == {} and live.read_engine() == {}
    assert not _under_real_results(backup.backup_dir())
    assert not config.RESULTS_DIR.joinpath("never").exists()


def test_snapshot_diff_sees_created_changed_and_removed_files(tmp_path):
    root = tmp_path / "results"
    assert conftest.snapshot_tree(root) is None and conftest.snapshot_diff(None, None) == []
    root.mkdir()
    (root / "a.json").write_text("1")
    (root / "b.json").write_text("1")
    before = conftest.snapshot_tree(root)
    assert conftest.snapshot_diff(before, conftest.snapshot_tree(root)) == []
    (root / "a.json").write_text("22")
    (root / "b.json").unlink()
    (root / "c.json").write_text("3")
    assert conftest.snapshot_diff(before, conftest.snapshot_tree(root)) == ["changed: a.json", "created: c.json", "removed: b.json"]
    assert conftest.snapshot_diff(None, conftest.snapshot_tree(root))[0] == "folder was created"


def test_session_guard_fails_the_run_when_a_real_results_folder_changed(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "real-results"
    fake.mkdir()
    (fake / "ledger.db").write_text("x")
    monkeypatch.setattr(conftest, "_REAL_BEFORE", {fake: conftest.snapshot_tree(fake)})

    class Session:
        exitstatus = 0
    conftest.pytest_sessionfinish(Session, 0)
    assert Session.exitstatus == 0
    (fake / "trading_mode.json").write_text("{}")
    conftest.pytest_sessionfinish(Session, 0)
    assert Session.exitstatus == 1 and "created: trading_mode.json" in capsys.readouterr().err
