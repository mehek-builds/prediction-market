import time

import pytest
from fastapi.testclient import TestClient

from fastlane import api
from fastlane.books import Book


@pytest.fixture
def seeded(tmp_ledger, tmp_path, monkeypatch):
    now = time.time()
    L = tmp_ledger
    L.event({"id": "e1", "source": "cnbc", "headline": "Live news", "url": "http://x", "published_ts": now - 110,
             "seen_ts": now - 105})
    L.event({"id": "e2", "source": "inject", "headline": "Test news", "seen_ts": now - 55, "synthetic": True})
    L.decision("e1", total_ms=400, jev_ms=300, action="BUY_YES", reason="signal_yes", venue="kalshi", market_id="MK1",
               market_question="Q1?", market_conf=.9, p_up=.9, p_down=.05, materiality=.6, mid_at_decision=.40,
               decided_ts=now - 100)
    L.decision("e2", total_ms=None, action="BUY_YES", reason="signal_yes", venue="kalshi", market_id="MK2",
               market_question="Q2?", p_up=.9, p_down=.05, mid_at_decision=.40, decided_ts=now - 50)
    L.trade(event_id="e1", opened_ts=now - 100, venue="kalshi", market_id="MK1", market_question="Q1?", side="yes",
            contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=0)
    L.trade(event_id="e2", opened_ts=now - 50, venue="kalshi", market_id="MK2", market_question="Q2?", side="yes",
            contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=1)
    L.mark("e1", 0, .42, .40, .41)
    L.mark("e1", 5, .47, .45, .46)

    async def fake(market):
        return Book("kalshi", market["id"], [(.62, 1)], [(.50, 1)])

    monkeypatch.setattr(api, "DB_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    monkeypatch.setattr(api, "_book", fake)
    api._books.clear()
    api._markets.clear()
    return TestClient(api.app), L, now


def test_trades(seeded):
    c, _, _ = seeded
    r = c.get("/trades")
    assert r.status_code == 200
    d = r.json()
    assert [t["synthetic"] for t in d["trades"]] == [True, False]  # newest first
    live = d["trades"][1]
    assert live["now_price"] == pytest.approx(.50) and live["value"] == pytest.approx(5.0)
    assert live["pnl"] == pytest.approx(.95) and live["move_cents"] == pytest.approx(10)
    ts = [p["ts"] for p in live["path"]]
    assert ts == sorted(ts) and live["path"][-1]["price"] == pytest.approx(.50)
    assert len(live["path"]) == 3  # entry, +5s mark (horizon 0 excluded), live point
    s = d["summary"]
    assert (s["trades"], s["live_trades"], s["test_trades"]) == (2, 1, 1)
    assert s["median_decision_ms"] == 400  # one decision has null total_ms: must not raise


def test_exclude_synthetic(seeded):
    c, _, _ = seeded
    assert len(c.get("/trades?include_synthetic=false").json()["trades"]) == 1


def test_book_unavailable(seeded, monkeypatch):
    c, _, _ = seeded

    async def none(market):
        return None
    monkeypatch.setattr(api, "_book", none)
    r = c.get("/trades")
    assert r.status_code == 200
    for t in r.json()["trades"]:
        assert t["now_price"] is None and t["value"] is None and t["pnl"] is None


def test_decisions(seeded):
    c, L, now = seeded
    L.event({"id": "e3", "source": "cnbc", "headline": "Bearish", "seen_ts": now - 20})
    L.decision("e3", total_ms=300, action="PASS", reason="weak_signal", venue="kalshi", market_id="MK3",
               market_question="Q3?", p_up=.1, p_down=.8, mid_at_decision=.50, decided_ts=now - 20)
    L.mark("e3", 5, .56, .54, .55)
    L.mark("e1", 30, .57, .53, .55)  # e1 leans yes, mid .40 -> .55
    d = c.get("/decisions").json()["decisions"]
    assert [x["id"] for x in d] == ["e3", "e1"]  # synthetic excluded, newest first
    by = {x["id"]: x for x in d}
    assert by["e1"]["move_cents_our_way"] == pytest.approx(15) and by["e1"]["lean"] == "yes"
    assert by["e3"]["move_cents_our_way"] == pytest.approx(-5) and by["e3"]["lean"] == "no"
    assert len(c.get("/decisions?limit=1").json()["decisions"]) == 1


def test_health_and_index(seeded):
    c, _, _ = seeded
    assert c.get("/health").json() == {"ok": True, "ledger": True}
    r = c.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<title>Fast lane" in r.text


def test_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    c = TestClient(api.app)
    r = c.get("/trades")
    assert r.status_code == 200 and r.json()["trades"] == []
    r = c.get("/decisions")
    assert r.status_code == 200 and r.json()["decisions"] == []
    assert c.get("/health").json()["ledger"] is False


def test_no_mutating_routes(seeded):
    c, _, _ = seeded
    for m in ("post", "put", "delete", "patch"):
        assert getattr(c, m)("/trades").status_code == 405


def test_no_cors_header(seeded):
    client, _, _ = seeded
    r = client.get("/trades", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_decisions_limit_is_clamped(seeded):
    client, _, _ = seeded
    assert client.get("/decisions?limit=-1").status_code == 200
    assert len(client.get("/decisions?limit=-1").json()["decisions"]) == 1  # clamped to 1, not "all rows"
    assert client.get("/decisions?limit=100000").status_code == 200


def test_corrupt_universe_cache_does_not_500(seeded, tmp_path, monkeypatch):
    client, _, _ = seeded
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(api, "CACHE", bad)
    assert client.get("/trades").status_code == 200


def test_foreign_host_header_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    c = TestClient(api.app)
    assert c.get("/health", headers={"host": "evil.example.net"}).status_code == 400
    assert c.get("/health", headers={"host": "localhost:8787"}).status_code == 200
