import pytest

from fastlane import engine
from fastlane.engine import freshness_block
from fastlane.ledger import Ledger


def ev(age=10, synthetic=False, pub=1000.0):
    return {"synthetic": synthetic, "published_ts": pub, "seen_ts": (pub + age) if pub is not None else 1000.0}


def test_synthetic_never_blocked():
    assert freshness_block(ev(age=9999, synthetic=True), .5, .9, "yes") is None


def test_stale_boundary():
    assert freshness_block(ev(age=601), None, None, "yes") == "stale_news"
    assert freshness_block(ev(age=599), None, None, "yes") is None
    assert freshness_block(ev(pub=None), None, None, "yes") is None


def test_priced_in_yes_and_no():
    assert freshness_block(ev(), .50, .53, "yes") == "priced_in"
    assert freshness_block(ev(), .50, .52, "yes") is None
    assert freshness_block(ev(), .50, .47, "no") == "priced_in"
    assert freshness_block(ev(), .50, .53, "no") is None  # moved against us


def test_missing_mids():
    assert freshness_block(ev(), None, .9, "yes") is None
    assert freshness_block(ev(), .1, None, "yes") is None


def test_engine_import_needs_no_env():
    assert engine.MAX_NEWS_AGE_S == 600  # import happened with autouse env cleared


def test_engine_requires_openrouter_key(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    with pytest.raises(RuntimeError):
        engine.Engine()


def test_engine_tape_disabled_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    e = engine.Engine()
    assert e.tape_enabled is False and e.tape.sign_headers is None
    assert "tape off (no Kalshi key)" in e.status()


def test_engine_tape_enabled_with_key(tmp_path, monkeypatch, rsa_pem):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "k")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", rsa_pem)
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    e = engine.Engine()
    assert e.tape_enabled is True and e.tape.sign_headers is not None


def test_engine_x_feed_off_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    e = engine.Engine()
    assert e.xfeed.enabled is False and e.bsky.enabled is True and "x off" in e.status()


def test_engine_rejects_invalid_handles(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy"); monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("XAI_X_HANDLES", "a,b,c,d,e,f,g,h,i,j,k")
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    with pytest.raises(ValueError):
        engine.Engine()


def _run_cli(env_extra):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "OPENROUTER_API_KEY": "dummy", "XAI_API_KEY": "", **env_extra}
    return subprocess.run([sys.executable, "-m", "fastlane.run", "--inject", "x", "--minutes", "0.01"], cwd=root,
                          env=env, capture_output=True, text=True, timeout=60)


def test_run_exits_on_invalid_move_deny_regex():
    r = _run_cli({"MOVE_DENY_RE": "(unclosed"})
    assert r.returncode != 0 and "MOVE_DENY_RE" in (r.stderr + r.stdout)


def test_run_exits_on_invalid_xai_settings():
    r = _run_cli({"XAI_API_KEY": "k", "XAI_X_HANDLES": "a,b,c,d,e,f,g,h,i,j,k"})
    assert r.returncode != 0 and "XAI_X_HANDLES" in (r.stderr + r.stdout)


def test_run_exits_on_invalid_bsky_handles():
    r = _run_cli({"BSKY_HANDLES": "not a handle"})
    assert r.returncode != 0 and "BSKY" in (r.stderr + r.stdout) and "Traceback" not in (r.stderr + r.stdout)


def test_stop_survives_a_failing_close_and_still_backs_up(monkeypatch, tmp_path):
    """One feed failing to close must not skip the other closes or the shutdown backup."""
    import asyncio
    from fastlane import backup
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    db = tmp_path / "ledger.db"
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(db))
    monkeypatch.setattr(backup, "DB_PATH", db)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "b"))
    e = engine.Engine(workers=1, verbose=False)
    closed = []

    async def boom():
        raise RuntimeError("feed close failed")

    async def ok():
        closed.append("jev")
    monkeypatch.setattr(e.xfeed, "aclose", boom)
    monkeypatch.setattr(e.jev, "aclose", ok)
    asyncio.run(e.stop())
    assert closed == ["jev"] and e.http.is_closed
    assert backup.list_backups(tmp_path / "b")
