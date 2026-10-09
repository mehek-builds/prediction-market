"""The Vercel bundle: right files, no secrets, password-protected, read-only. The Vercel CLI is never called."""
import base64
import json
import subprocess
import sys
import time

import pytest

from fastlane import deploy
from fastlane.ledger import Ledger
from fastlane.passwords import check_password, hash_password


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "ledger.db")
    now = time.time()
    led.event({"id": "e1", "source": "s", "headline": "Private headline", "seen_ts": now})
    led.decision("e1", action="BUY_YES", reason="signal_yes", venue="kalshi", market_id="KX-1", decided_ts=now)
    led.trade(event_id="e1", opened_ts=now, venue="kalshi", market_id="KX-1", market_question="q", side="yes",
              contracts=1, avg_price=.5, cost=.5, fee=.02, best_ask=.5, synthetic=0)
    uni = tmp_path / "universe.json"
    uni.write_text(json.dumps([{"id": "KX-1", "venue": "kalshi"}, {"id": "OTHER", "venue": "kalshi"}]))
    monkeypatch.setattr(deploy, "CACHE", uni)
    out = deploy.build("fastlane-dashboard-test", hash_password("hunter22"), led.path, tmp_path / "vercel")
    return out


def test_bundle_layout(bundle):
    for rel in ["api/index.py", "vercel.json", "requirements.txt", "fastlane/hosted.py", "fastlane/api.py",
                "fastlane/static/index.html", "fastlane/results/ledger.db", "fastlane/hosted_auth.json"]:
        assert (bundle / rel).exists(), rel
    assert "pytest" not in (bundle / "requirements.txt").read_text()
    assert json.loads((bundle / "vercel.json").read_text())["rewrites"][0]["destination"] == "/api/index"
    assert [m["id"] for m in json.loads((bundle / "fastlane/results/universe.json").read_text())] == ["KX-1"]
    assert not (bundle / "fastlane/results/ledger.db-wal").exists()


def test_nothing_is_served_statically_but_the_empty_public_folder(bundle):
    cfg = json.loads((bundle / "vercel.json").read_text())
    assert cfg["outputDirectory"] == "public"
    assert [p.name for p in (bundle / "public").iterdir()] == ["robots.txt"]
    assert cfg["functions"]["api/index.py"]["includeFiles"] == "fastlane/**"


def test_stray_secrets_in_the_package_never_ship(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    import shutil
    shutil.copytree(deploy.PKG, pkg, ignore=shutil.ignore_patterns("results", "__pycache__"))
    for name in (".env", "kalshi.pem", "x.key"):
        (pkg / name).write_text("secret")
    monkeypatch.setattr(deploy, "PKG", pkg)
    out = deploy.build("p", hash_password("x" * 12), tmp_path / "none.db", tmp_path / "v")
    shipped = {p.name for p in (out / "fastlane").rglob("*")}
    assert not {".env", "kalshi.pem", "x.key"} & shipped


def test_bundle_has_no_secrets_or_plain_password(bundle):
    auth = json.loads((bundle / "fastlane/hosted_auth.json").read_text())
    assert set(auth) == {"salt", "iterations", "hash"} and check_password("hunter22", auth)
    for f in bundle.rglob("*"):
        if f.is_file() and f.suffix != ".db":
            text = f.read_bytes()
            assert b"hunter22" not in text and b"sk-or-v1-" not in text, f
    assert not list(bundle.rglob(".env"))


def test_rebuild_keeps_the_vercel_link(bundle, tmp_path):
    (bundle / ".vercel").mkdir()
    (bundle / ".vercel" / "project.json").write_text("{}")
    deploy.build("fastlane-dashboard-test", hash_password("x"), tmp_path / "ledger.db", tmp_path / "vercel")
    assert (bundle / ".vercel" / "project.json").exists()


def test_fingerprint_changes_with_new_rows(tmp_path):
    led = Ledger(tmp_path / "l.db")
    a = deploy.ledger_fingerprint(led.path)
    led.event({"id": "e9", "source": "s", "headline": "h", "seen_ts": 1.0})
    assert deploy.ledger_fingerprint(led.path) != a


def test_setup_generates_password_once(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "STATE_FILE", tmp_path / "vercel.json")
    state, shown = deploy.setup(None, None)
    assert shown and state["project"].startswith("fastlane-dashboard-") and check_password(shown, state["password_hash"])
    assert (tmp_path / "vercel.json").stat().st_mode & 0o077 == 0
    state2, shown2 = deploy.setup(None, None)
    assert shown2 is None and state2["project"] == state["project"]
    with pytest.raises(deploy.DeployError):
        deploy.setup("Bad Name!", None)
    with pytest.raises(deploy.DeployError, match="12 characters"):
        deploy.setup(None, "short")


HOSTED_CHECK = r"""
import base64, json, sys
from starlette.testclient import TestClient
from api.index import app
c = TestClient(app)
ok = {"Authorization": "Basic " + base64.b64encode(b"any:hunter22").decode()}
bad = {"Authorization": "Basic " + base64.b64encode(b"any:wrong").decode()}
out = {
  "noauth": c.get("/trades").status_code,
  "bad": c.get("/trades", headers=bad).status_code,
  "ok": c.get("/trades", headers=ok).status_code,
  "trades": len(c.get("/trades", headers=ok).json()["trades"]),
  "index": c.get("/", headers=ok).status_code,
  "state": c.get("/control/state", headers=ok).json(),
  "post": c.post("/control/mode", headers=ok, json={"mode": "live"}).status_code,
  "shadow_post": c.post("/settings/shadow", headers={**ok, "X-Fastlane-Control": "1", "Content-Type": "application/json"}, content='{"enabled": false}').status_code,
  "mode_post": c.post("/control/mode", headers={**ok, "X-Fastlane-Control": "1", "Content-Type": "application/json"}, content='{"mode": "paper"}').status_code,
  "html_has_mode_cell": 'id="st-mode"' in c.get("/", headers=ok).text,
  "host_ok": c.get("/health", headers={**ok, "host": "x.vercel.app"}).status_code,
  "host_bad": c.get("/health", headers={**ok, "host": "evil.example.com"}).status_code,
}
print(json.dumps(out))
"""


def _hosted(bundle):
    p = subprocess.run([sys.executable, "-c", HOSTED_CHECK], cwd=bundle, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "FASTLANE_TELEMETRY": "0"}, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_hosted_app_is_password_protected_and_read_only(bundle):
    r = _hosted(bundle)
    assert r["noauth"] == 401 and r["bad"] == 401 and r["ok"] == 200 and r["index"] == 200
    assert r["trades"] == 1
    assert r["state"]["hosted"] is True and "token" not in r["state"] and r["state"]["mode"] == "paper"
    assert r["state"]["snapshot_ts"]
    assert r["post"] == 404 and r["shadow_post"] == 404 and r["mode_post"] == 404
    assert r["html_has_mode_cell"] is True
    assert r["host_ok"] == 200 and r["host_bad"] == 400


def test_hosted_fails_closed_without_password_file(bundle):
    (bundle / "fastlane/hosted_auth.json").unlink()
    p = subprocess.run([sys.executable, "-c", "from starlette.testclient import TestClient\nfrom api.index import app\n"
                        "print(TestClient(app).get('/trades').status_code)"], cwd=bundle, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "FASTLANE_TELEMETRY": "0"}, timeout=60)
    assert p.stdout.strip().endswith("503"), p.stderr[-1000:]


def test_watch_interval_has_a_floor(monkeypatch):
    calls = []

    class Stop:
        def is_set(self):
            return len(calls) > 0

        def wait(self, s):
            calls.append(s)
    monkeypatch.setattr(deploy, "sync", lambda: None)
    deploy.watch(1, Stop())
    assert calls == [deploy.MIN_WATCH_MINUTES * 60]
