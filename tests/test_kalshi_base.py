"""KALSHI_BASE_URL: one host for every Kalshi call, validated, and the demo order check cannot touch production."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fastlane import config
from fastlane.ledger import Ledger
from fastlane.live import LiveTrader

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("demo_no_order_check", ROOT / "tools" / "demo_no_order_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_is_production_everywhere():
    assert config.kalshi_base_url() == "https://api.elections.kalshi.com"
    assert config.kalshi_api_url() == "https://api.elections.kalshi.com/trade-api/v2"
    assert config.kalshi_ws_url() == "wss://api.elections.kalshi.com/trade-api/ws/v2"
    assert not config.kalshi_is_demo()


def test_demo_host_rest_and_websocket(monkeypatch):
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co/")
    assert config.kalshi_api_url() == "https://demo-api.kalshi.co/trade-api/v2"
    assert config.kalshi_ws_url() == "wss://demo-api.kalshi.co/trade-api/ws/v2"
    assert config.kalshi_is_demo()


def test_other_hosts_are_rejected(monkeypatch):
    for bad in ("https://evil.example.com", "http://demo-api.kalshi.co", "https://demo-api.kalshi.co/x"):
        monkeypatch.setenv("KALSHI_BASE_URL", bad)
        with pytest.raises(ValueError):
            config.kalshi_base_url()
    monkeypatch.setenv("KALSHI_ALLOW_CUSTOM_BASE", "1")   # the old opt-in no longer exists
    monkeypatch.setenv("KALSHI_BASE_URL", "https://evil.example.com")
    with pytest.raises(ValueError):
        config.kalshi_base_url()
    assert "KALSHI_ALLOW_CUSTOM_BASE" not in (ROOT / "fastlane" / "config.py").read_text()


def test_engine_refuses_a_bad_base_url(monkeypatch, tmp_path):
    from fastlane import engine
    from fastlane.engine import Engine
    monkeypatch.setattr(engine, "Ledger", lambda: Ledger(tmp_path / "l.db"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("KALSHI_BASE_URL", "https://evil.example.com")
    with pytest.raises(ValueError):
        Engine()


def test_results_dir_is_per_exchange(monkeypatch):
    prod = config.PKG / "results"
    assert config.results_dir() == prod
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    assert config.results_dir() == config.PKG / "results-demo"
    monkeypatch.setenv("KALSHI_BASE_URL", "https://evil.example.com")
    assert config.results_dir() == prod          # the engine refuses to start on it; nothing is written to demo
    assert "fastlane/results-demo/" in (ROOT / ".gitignore").read_text().split()


def _import_dirs(env):
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from fastlane import config, ledger, universe, live, backup; "
            "print(config.RESULTS_DIR.name, ledger.DB_PATH.parent.name, universe.CACHE.parent.name, "
            "live.MODE_FILE.parent.name, live.ENGINE_FILE.parent.name, backup.backup_dir().parent.name)")
    # Isolated: -I ignores PYTHONPATH and user site; only PATH-free essentials plus the explicit env, and a copy of the
    # package without a developer .env (load_env only fills variables that are unset, so BACKUP_DIR and KALSHI_* are
    # pinned explicitly below).
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT", "LANG")}
    full = {**base, "BACKUP_DIR": "", "KALSHI_BASE_URL": "", **env}
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, env=full)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_every_data_path_follows_the_exchange_at_import():
    assert set(_import_dirs({})) == {"results"}
    assert set(_import_dirs({"KALSHI_BASE_URL": "https://demo-api.kalshi.co"})) == {"results-demo"}


def test_every_kalshi_call_follows_the_override(monkeypatch):
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    t = LiveTrader.__new__(LiveTrader)
    LiveTrader.__init__(t, ledger=None)
    assert t.base_url == "https://demo-api.kalshi.co"
    for name in ("books.py", "universe.py", "engine.py", "kalshi_tape.py", "live.py"):
        text = (ROOT / "fastlane" / name).read_text()
        assert "api.elections.kalshi.com" not in text, name


def test_demo_script_refuses_production(monkeypatch, capsys):
    mod = _load()
    assert not (ROOT / "fastlane" / "demo_no_order_check.py").exists()
    for base in (None, "https://api.elections.kalshi.com", "https://evil.example.com"):
        if base:
            monkeypatch.setenv("KALSHI_BASE_URL", base)
        else:
            monkeypatch.delenv("KALSHI_BASE_URL", raising=False)
        monkeypatch.setattr(mod, "place_one", lambda *a, **k: pytest.fail("must not place an order"))
        monkeypatch.setattr(mod, "signed_get", lambda *a, **k: pytest.fail("must not call the exchange"))
        for extra in ([], ["--fill"]):
            assert mod.main(["--ticker", "X", "--yes"] + extra) == 2


def test_demo_script_builds_one_no_order_and_defaults_to_a_dry_run(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    monkeypatch.setattr(mod, "place_one", lambda *a, **k: pytest.fail("dry run must not send"))
    monkeypatch.setattr(mod, "signed_get", lambda *a, **k: pytest.fail("dry run must not call the exchange"))
    assert mod.main(["--ticker", "T-1"]) == 0
    body = json.loads(capsys.readouterr().out.split("Body would be:\n")[1])
    assert body["side"] == "ask" and body["count"] == "1" and body["price"] == "0.9900"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert mod.main(["--ticker", "T-1", "--price-cents", "60", "--yes"]) == 2   # accept-only mode refuses a marketable price
    assert mod.main(["--ticker", "T-1", "--fill"]) == 0                          # --fill without --yes is a dry run too


def test_demo_script_with_yes_sends_once_on_demo(monkeypatch):
    mod = _load()
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    calls = []
    monkeypatch.setattr(mod, "place_one", lambda base, t, p: calls.append((base, t, p)) or {"http_status": 201})
    assert mod.main(["--ticker", "T-1", "--yes"]) == 0
    assert calls == [("https://demo-api.kalshi.co", "T-1", 1)]


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _fake_net(monkeypatch, mod, rsa_pem, positions_after, book=None):
    """No network: fake httpx.post/get and a recording signer. Returns the list of (kind, url, signed_path, params)."""
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "demo-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", rsa_pem)
    from fastlane.kalshi import KalshiClient
    log, signed, state = [], [], {"posted": False}
    real = KalshiClient.sign_headers

    def sign(self, method, path):
        signed.append((method, path))
        return real(self, method, path)

    monkeypatch.setattr(KalshiClient, "sign_headers", sign)

    def post(url, json=None, headers=None, timeout=None):
        assert headers["KALSHI-ACCESS-KEY"] == "demo-key" and headers["KALSHI-ACCESS-SIGNATURE"]
        log.append(("post", url, json))
        state["posted"] = True
        return _Resp(201, {"order": {"status": "executed"}})

    def get(url, params=None, headers=None, timeout=None):
        assert headers["KALSHI-ACCESS-SIGNATURE"]
        log.append(("get", url, params))
        if url.endswith("/orderbook"):
            return _Resp(200, book or {"orderbook": {"yes": [[40, 5], [62, 3]], "no": [[30, 2], [36, 9]]}})
        return _Resp(200, positions_after if state["posted"] else {"market_positions": []})

    monkeypatch.setattr(mod.httpx, "post", post)
    monkeypatch.setattr(mod.httpx, "get", get)
    return log, signed


def test_demo_script_order_goes_to_the_demo_url_with_a_signed_order_path(monkeypatch, rsa_pem):
    mod = _load()
    log, signed = _fake_net(monkeypatch, mod, rsa_pem, {})
    out = mod.place_one("https://demo-api.kalshi.co", "T-1", 1)
    assert log == [("post", "https://demo-api.kalshi.co/trade-api/v2/portfolio/events/orders", out["request"])]
    assert signed == [("POST", "/trade-api/v2/portfolio/events/orders")]
    assert out["request"]["side"] == "ask" and out["http_status"] == 201


def test_demo_script_fill_passes_only_on_a_plus_one_no_position(monkeypatch, rsa_pem, capsys):
    mod = _load()
    pos = {"market_positions": [{"ticker": "T-1", "position": -1}]}
    log, signed = _fake_net(monkeypatch, mod, rsa_pem, pos)
    assert mod.main(["--ticker", "T-1", "--fill", "--yes"]) == 0
    posts = [l for l in log if l[0] == "post"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["side"] == "ask" and body["price"] == "0.6200"     # NO limit 38c = a YES ask at the 62c best bid
    assert ("GET", "/trade-api/v2/portfolio/positions") in signed   # signed path carries no query string
    assert [l[2] for l in log if l[1].endswith("/portfolio/positions")][-1] == {"ticker": "T-1"}
    out = capsys.readouterr().out
    assert "PASS" in out and '"position": -1' in out                # the raw JSON is printed


@pytest.mark.parametrize("pos", [{"market_positions": [{"ticker": "T-1", "position": 1}]},
                                 {"market_positions": []}, {"market_positions": [{"ticker": "T-1", "position": 0}]},
                                 {"unexpected": True}])
def test_demo_script_fill_fails_on_any_other_position(monkeypatch, rsa_pem, pos):
    mod = _load()
    _fake_net(monkeypatch, mod, rsa_pem, pos)
    assert mod.main(["--ticker", "T-1", "--fill", "--yes"]) == 1


def test_demo_script_fill_refuses_without_a_bid_and_close_buys_yes_once(monkeypatch, rsa_pem):
    mod = _load()
    log, _ = _fake_net(monkeypatch, mod, rsa_pem, {}, book={"orderbook": {"yes": [], "no": []}})
    assert mod.main(["--ticker", "T-1", "--fill", "--yes"]) == 1
    assert not [l for l in log if l[0] == "post"]
    pos = {"market_positions": [{"ticker": "T-1", "position_fp": "-1.00"}]}
    log, _ = _fake_net(monkeypatch, mod, rsa_pem, pos)
    assert mod.main(["--ticker", "T-1", "--fill", "--close", "--yes"]) == 0
    sides = [l[2]["side"] for l in log if l[0] == "post"]
    assert sides == ["ask", "bid"]


def test_demo_script_book_and_position_parsing_is_defensive():
    mod = _load()
    assert mod.best_bids({"orderbook": {"yes": [[10, 1], [55, 2]], "no": None}}) == (55, None)
    assert mod.best_bids({"orderbook_fp": {"yes_dollars": [["0.5500", "2"]], "no_dollars": [["0.3000", "1"]]}}) == (55, 30)
    assert mod.position_of({"market_positions": [{"ticker": "A", "position": -1}]}, "A") == -1
    assert mod.position_of({"market_positions": []}, "A") == 0
    assert mod.position_of({}, "A") is None


def test_close_only_when_the_position_is_exactly_minus_one(monkeypatch, rsa_pem, capsys):
    mod = _load()
    pos = {"market_positions": [{"ticker": "T-1", "position": 1}]}
    log, _ = _fake_net(monkeypatch, mod, rsa_pem, pos)
    assert mod.main(["--ticker", "T-1", "--fill", "--close", "--yes"]) == 1
    assert [l[2]["side"] for l in log if l[0] == "post"] == ["ask"]     # no closing order
    assert "not closing" in capsys.readouterr().out


def test_vercel_publish_is_refused_on_the_demo_host(monkeypatch, capsys):
    from fastlane import deploy
    monkeypatch.setenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    with pytest.raises(deploy.DeployError, match="demo host"):
        deploy.sync(force=True, dry_run=True)
    assert deploy.main(["--dry-run"]) == 1
    assert "demo host" in capsys.readouterr().err
