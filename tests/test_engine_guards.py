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
