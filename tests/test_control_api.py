"""The dashboard's paper / real switch: token, same-origin, engine running, enabled, exact phrase."""
import time

import pytest
from fastapi.testclient import TestClient

from fastlane import api, live


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    return TestClient(api.app, client=("127.0.0.1", 50000))


def _engine(enabled=True, kalshi=True, age=0):
    live._write_json(live.ENGINE_FILE, {"session": "s1", "heartbeat_ts": time.time() - age, "live_enabled": enabled,
                                        "kalshi_configured": kalshi, "limits": live.limits()})


def _post(c, body, token=None, **headers):
    tok = token if token is not None else c.get("/control/state").json()["token"]
    return c.post("/control/mode", json=body, headers={"X-Fastlane-Token": tok, "X-Fastlane-Control": "1", **headers})


CTRL = {"X-Fastlane-Control": "1"}   # the write guard shared with POST /settings/shadow
ARM = {"mode": "live", "confirm": live.CONFIRM_PHRASE, "session": "s1"}


def test_state_defaults_to_paper(client):
    s = client.get("/control/state").json()
    assert s["mode"] == "paper" and not s["engine_running"] and s["token"] and s["orders"] == []
    assert s["confirm_phrase"] == live.CONFIRM_PHRASE


def test_arm_happy_path_then_back_to_paper(client):
    _engine()
    r = _post(client, ARM)
    assert r.status_code == 200 and r.json()["mode"] == "live"
    assert live.read_mode() == {**live.read_mode(), "mode": "live", "session": "s1"}
    r = _post(client, {"mode": "paper"})
    assert r.status_code == 200 and r.json()["mode"] == "paper"


def test_missing_or_wrong_token_cannot_arm(client):
    _engine()
    assert _post(client, ARM, token="nope").status_code == 403
    assert client.post("/control/mode", json=ARM).status_code == 403
    assert live.read_mode() == {}


def test_paper_needs_no_token_so_a_stale_page_can_always_stop(client):
    _engine()
    _post(client, ARM)
    r = client.post("/control/mode", json={"mode": "paper"}, headers=CTRL)
    assert r.status_code == 200 and r.json()["mode"] == "paper"


def test_arming_refused_if_engine_restarted_since_page_load(client):
    _engine()
    r = _post(client, {**ARM, "session": "older-session"})
    assert r.status_code == 409 and any("restarted" in p for p in r.json()["problems"])


def test_polling_never_starves_the_switch(client):
    _engine()
    for _ in range(40):
        assert client.get("/control/state").status_code == 200
    assert _post(client, ARM).status_code == 200
    assert client.post("/control/mode", json={"mode": "paper"}, headers=CTRL).status_code == 200


def test_cross_origin_is_rejected(client):
    _engine()
    assert _post(client, ARM, origin="https://evil.example.com").status_code == 403
    assert _post(client, ARM, origin="http://testserver").status_code == 200


@pytest.mark.parametrize("engine_kw,body,needle", [
    ({"age": 600}, ARM, "not running"),
    ({"enabled": False}, ARM, "LIVE_TRADING_ENABLED"),
    ({"kalshi": False}, ARM, "Kalshi API key"),
    ({}, {"mode": "live", "confirm": "trade real money", "session": "s1"}, "exactly"),
])
def test_arming_refused_with_reasons(client, engine_kw, body, needle):
    _engine(**engine_kw)
    r = _post(client, body)
    assert r.status_code == 409 and any(needle in p for p in r.json()["problems"])
    assert live.read_mode().get("mode") != "live"


def test_paper_always_allowed_even_without_engine(client):
    assert _post(client, {"mode": "paper"}).status_code == 200


def test_bad_mode_value(client):
    assert _post(client, {"mode": "yolo"}).status_code == 400


def test_stale_session_reads_as_paper(client):
    _engine()
    live.arm("an-old-session")
    assert client.get("/control/state").json()["mode"] == "paper"


def test_paper_needs_the_guard_but_no_token(client):
    assert client.post("/control/mode", json={"mode": "paper"}, headers=CTRL).status_code == 200
    assert client.post("/control/mode", json={"mode": "paper"}).status_code == 403


def test_arm_rejects_extra_keys(client):
    _engine()
    assert _post(client, {**ARM, "x": 1}).status_code == 400
    assert live.read_mode() == {}


@pytest.mark.parametrize("phrase", ["TRADE REAL MONEY ", " TRADE REAL MONEY", "trade real money", "TRADE  REAL MONEY", ""])
def test_arm_phrase_is_exact(client, phrase):
    _engine()
    assert _post(client, {**ARM, "confirm": phrase}).status_code == 409
    assert live.read_mode().get("mode") != "live"


@pytest.mark.parametrize("missing", ["header", "token", "phrase", "session", "engine", "enabled", "kalshi"])
def test_each_missing_arming_condition_blocks_alone(client, missing):
    """Every condition is required separately; the other six are satisfied in each case."""
    _engine(enabled=missing != "enabled", kalshi=missing != "kalshi", age=600 if missing == "engine" else 0)
    tok = client.get("/control/state").json()["token"]
    headers = {} if missing == "header" else {"X-Fastlane-Control": "1"}
    if missing != "token":
        headers["X-Fastlane-Token"] = tok
    body = {"mode": "live", "confirm": "nope" if missing == "phrase" else live.CONFIRM_PHRASE,
            "session": "stale" if missing == "session" else "s1"}
    assert client.post("/control/mode", json=body, headers=headers).status_code in (403, 409)
    assert live.read_mode().get("mode") != "live"


def test_all_conditions_together_do_arm(client):
    _engine()
    assert _post(client, ARM).status_code == 200 and live.read_mode()["mode"] == "live"


def test_token_and_arming_are_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    _engine()
    remote = TestClient(api.app, client=("203.0.113.9", 50000))
    local = TestClient(api.app, client=("127.0.0.1", 50000))
    assert "token" not in remote.get("/control/state").json()
    tok = local.get("/control/state").json()["token"]
    h = {"X-Fastlane-Token": tok, "X-Fastlane-Control": "1"}
    r = remote.post("/control/mode", json=ARM, headers=h)             # even with the right token
    assert r.status_code == 403 and r.json()["error"] == "loopback_only"
    assert live.read_mode().get("mode") != "live"
    # paper always works from anywhere
    assert remote.post("/control/mode", json={"mode": "paper"}, headers={"X-Fastlane-Control": "1"}).status_code == 200
    assert local.post("/control/mode", json=ARM, headers=h).status_code == 200
    assert TestClient(api.app, client=("::1", 1)).get("/control/state").json().get("token")


def test_remote_control_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setenv("LIVE_ALLOW_REMOTE_CONTROL", "1")
    _engine()
    remote = TestClient(api.app, client=("203.0.113.9", 50000))
    tok = remote.get("/control/state").json()["token"]
    r = remote.post("/control/mode", json=ARM, headers={"X-Fastlane-Token": tok, "X-Fastlane-Control": "1"})
    assert r.status_code == 200


def test_non_ascii_token_header_is_403_not_500(client):
    _engine()
    r = client.post("/control/mode", json=ARM, headers={"X-Fastlane-Control": "1",
                                                        "X-Fastlane-Token": "caf\u00e9".encode("utf-8")})
    assert r.status_code == 403


def test_remote_post_never_returns_the_token():
    remote = TestClient(api.app, client=("203.0.113.9", 5000))
    assert "token" not in remote.get("/control/state").json()
    r = remote.post("/control/mode", json={"mode": "paper"}, headers=CTRL)
    assert r.status_code == 200 and "token" not in r.json()
    r = remote.post("/control/mode", json=ARM, headers={**CTRL, "X-Fastlane-Token": "x"})
    assert r.status_code == 403 and "token" not in r.text


def test_loopback_post_paper_returns_the_token(client):
    r = client.post("/control/mode", json={"mode": "paper"}, headers=CTRL)
    assert r.status_code == 200 and r.json()["token"]


def test_remote_post_with_remote_control_allowed_gets_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setenv("LIVE_ALLOW_REMOTE_CONTROL", "1")
    remote = TestClient(api.app, client=("203.0.113.9", 5000))
    assert remote.post("/control/mode", json={"mode": "paper"}, headers=CTRL).json()["token"]
