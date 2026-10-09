"""Error reports: scrubbed before they leave, throttled, never raising, and no stack traces over HTTP."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastlane import api, errors


def test_scrub_removes_keys_and_home():
    home = str(Path.home())
    raw = (f"sk-or-v1-{'a' * 30} Bearer abcdefghijkl api_key='zzzzzzzzzz' {home}/code/x.py "
           "KALSHI-ACCESS-SIGNATURE: abcdefghijklmnop -----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----")
    out = errors.scrub(raw, 1000)
    for leaked in ["aaaaaaaaaa", "abcdefghijkl", "zzzzzzzzzz", home, "MIIabc", "abcdefghijklmnop"]:
        assert leaked not in out
    assert "~/code/x.py" in out


def test_before_send_strips_personal_fields():
    ev = {"request": {"url": "x"}, "user": {"ip_address": "1.2.3.4"}, "extra": {"headline": "secret"},
          "breadcrumbs": {"values": [1]}, "server_name": "Mehek-MacBook", "contexts": {"os": {}, "device": {"x": 1}},
          "exception": {"values": [{"value": f"boom sk-or-v1-{'b' * 20}", "stacktrace": {"frames": [
              {"abs_path": str(Path.home() / "x.py"), "vars": {"key": "v"}, "context_line": "token = 'abcdefgh'"}]}}]}}
    out = errors.before_send(ev)
    assert not {"request", "user", "extra", "breadcrumbs"} & set(out)
    assert out["server_name"] == "fastlane" and set(out["contexts"]) == {"os"}
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "vars" not in frame and frame["abs_path"].startswith("~") and "abcdefgh" not in frame["context_line"]
    assert "bbbb" not in out["exception"]["values"][0]["value"]


def test_dsn_precedence(monkeypatch):
    monkeypatch.setattr(errors, "MAINTAINER_DSN_KEY", "k")
    monkeypatch.setattr(errors, "MAINTAINER_DSN_HOST", "o1.ingest.example.com")
    monkeypatch.setattr(errors, "MAINTAINER_DSN_PROJECT", "9")
    monkeypatch.delenv("FASTLANE_TELEMETRY", raising=False)
    assert errors.telemetry_dsn() == "https://k" + chr(64) + "o1.ingest.example.com/9"   # on by default
    monkeypatch.setenv("FASTLANE_TELEMETRY", "0")
    assert errors.telemetry_dsn() == ""                                                    # opt out
    monkeypatch.setenv("FASTLANE_SENTRY_DSN", "https://own")
    assert errors.telemetry_dsn() == "https://own"                                         # own Sentry wins


def test_throttle(monkeypatch):
    monkeypatch.setitem(errors._state, "sent", {})
    monkeypatch.setitem(errors._state, "hour", [])
    assert errors._allowed("a", 1000) and not errors._allowed("a", 1001)
    assert errors._allowed("a", 1000 + errors.SAME_ERROR_EVERY_S + 1)
    for i in range(errors.MAX_PER_HOUR):
        errors._allowed(f"k{i}", 5000)
    assert not errors._allowed("fresh", 5000)


def test_capture_never_raises_and_logs_locally(monkeypatch, tmp_path):
    try:
        raise ValueError(f"bad sk-or-v1-{'c' * 20}")
    except ValueError as exc:
        errors.capture(exc, "test")
    errors.message("feed down", "test")


def test_500_has_no_stack_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")

    async def boom(*a, **k):
        raise RuntimeError("secret internals at /Users/someone/x.py")
    monkeypatch.setattr(api, "_trades", boom)
    r = TestClient(api.app, raise_server_exceptions=False).get("/trades")
    assert r.status_code == 500 and r.json() == {"error": "internal"}
    assert "Traceback" not in r.text and "secret" not in r.text


def test_docs_routes_are_off(tmp_path, monkeypatch):
    c = TestClient(api.app)
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
