"""The dashboard's paper / real switch: token, same-origin, engine running, enabled, exact phrase."""
import time

import pytest
from fastapi.testclient import TestClient

from fastlane import api, live


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    return TestClient(api.app)


def _engine(enabled=True, kalshi=True, age=0):
    live._write_json(live.ENGINE_FILE, {"session": "s1", "heartbeat_ts": time.time() - age, "live_enabled": enabled,
                                        "kalshi_configured": kalshi, "limits": live.limits()})


def _post(c, body, token=None, **headers):
    tok = token if token is not None else c.get("/control/state").json()["token"]
    return c.post("/control/mode", json=body, headers={"X-Fastlane-Token": tok, **headers})


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
    r = client.post("/control/mode", json={"mode": "paper"})
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
    assert client.post("/control/mode", json={"mode": "paper"}).status_code == 200


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
