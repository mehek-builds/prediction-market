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
            contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=0, signal_strength=.9)
    L.trade(event_id="e2", opened_ts=now - 50, venue="kalshi", market_id="MK2", market_question="Q2?", side="yes",
            contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=1)
    L.mark("e1", 0, .42, .40, .41)
    L.mark("e1", 5, .47, .45, .46)
    L.shadow_decision("e1", None, "real_signalled")
    L.event({"id": "e4", "source": "cnbc", "headline": "Shadow news", "url": "http://x", "published_ts": now - 40,
             "seen_ts": now - 30})
    L.decision("e4", total_ms=350, action="PASS", reason="weak_signal", venue="kalshi", market_id="MK4",
               market_question="Q4?", p_up=.65, p_down=.1, mid_at_decision=.40, decided_ts=now - 30)
    L.shadow_decision("e4", "BUY_YES", "signal_yes", "MK4")
    L.trade(event_id="e4", opened_ts=now - 30, venue="kalshi", market_id="MK4", market_question="Q4?", side="yes",
            contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=0, shadow=1,
            signal_strength=.65, signal_decisive=.3)
    L.mark("shadow:e4", 0, .42, .40, .41)
    L.mark("shadow:e4", 5, .47, .45, .46)

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
    assert [t["synthetic"] for t in d["trades"]] == [False, True, False]  # newest first: e4, e2, e1
    assert [t["shadow"] for t in d["trades"]] == [True, False, False]
    sh = d["trades"][0]
    assert sh["bucket"] == "0.60-0.70" and sh["signal_strength"] == pytest.approx(.65)
    assert len(sh["path"]) == 3   # entry, +5s shadow mark (proves mark_key), live point
    live = d["trades"][2]
    assert live["bucket"] == "0.85+"
    assert live["now_price"] == pytest.approx(.50) and live["value"] == pytest.approx(5.0)
    assert live["pnl"] == pytest.approx(.95) and live["move_cents"] == pytest.approx(10)
    ts = [p["ts"] for p in live["path"]]
    assert ts == sorted(ts) and live["path"][-1]["price"] == pytest.approx(.50)
    assert len(live["path"]) == 3  # entry, +5s mark (horizon 0 excluded), live point
    s = d["summary"]
    assert (s["trades"], s["live_trades"], s["shadow_trades"], s["test_trades"]) == (3, 1, 1, 1)
    assert s["pnl"] == pytest.approx(1.9)   # real book only (e1 + e2); shadow excluded
    assert s["books"]["shadow"]["pnl"] == pytest.approx(.95)
    b = s["books"]["shadow"]["buckets"]
    assert list(b) == ["0.60-0.70"] and b["0.60-0.70"]["trades"] == 1 and b["0.60-0.70"]["pnl"] == pytest.approx(.95)
    assert s["books"]["live"]["pnl"] == pytest.approx(.95)
    assert s["median_decision_ms"] == 400  # one decision has null total_ms: must not raise


def test_exclude_synthetic(seeded):
    c, _, _ = seeded
    assert len(c.get("/trades?include_synthetic=false").json()["trades"]) == 2   # live + shadow
    assert len(c.get("/trades?include_synthetic=false&book=live").json()["trades"]) == 1


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
    assert [x["id"] for x in d] == ["e3", "e4", "e1"]  # synthetic excluded, newest decision first (e3 now-20, e4 now-30)
    by = {x["id"]: x for x in d}
    assert by["e1"]["move_cents_our_way"] == pytest.approx(15) and by["e1"]["lean"] == "yes"
    assert by["e3"]["move_cents_our_way"] == pytest.approx(-5) and by["e3"]["lean"] == "no"
    assert by["e3"]["after_costs_cents"] is None   # only a +5s mark, no horizon-0 mark
    assert len(c.get("/decisions?limit=1").json()["decisions"]) == 1


def test_health_and_index(seeded):
    c, _, _ = seeded
    h = c.get("/health").json()
    assert h["ok"] is True and h["ledger"] is True
    assert h["x"] == {"enabled": False, "calls_today": 0, "spend_today_usd": 0.0, "budget_hit": False,
                      "in_window": False, "updated_ts": None}
    assert h["bluesky"] == {"connected": False, "mode": None, "updated_ts": None}
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


def test_book_filter(seeded):
    c, _, _ = seeded
    for book, n in (("all", 3), ("live", 1), ("shadow", 1), ("test", 1)):
        d = c.get(f"/trades?book={book}").json()
        assert len(d["trades"]) == n and d["summary"]["trades"] == n
    assert c.get("/trades?book=bogus").status_code == 422
    s = c.get("/trades?book=shadow").json()["summary"]
    assert s["pnl"] == 0 and s["books"]["shadow"]["trades"] == 1


def test_book_filter_skips_book_fetches_for_excluded_rows(seeded, monkeypatch):
    c, _, _ = seeded
    seen = []

    async def spy(market):
        seen.append(market["id"])
        return None
    monkeypatch.setattr(api, "_book", spy)
    api._books.clear()
    c.get("/trades?book=shadow")
    assert set(seen) <= {"MK4"}


def test_decisions_carry_shadow_fields(seeded):
    c, _, _ = seeded
    by = {x["id"]: x for x in c.get("/decisions").json()["decisions"]}
    assert by["e4"]["shadow_traded"] is True and by["e4"]["shadow_reason"] == "signal_yes"
    assert by["e4"]["shadow_market_id"] == "MK4"
    assert by["e1"]["shadow_traded"] is False and by["e1"]["shadow_reason"] == "real_signalled"


def test_decisions_after_costs(seeded):
    c, L, now = seeded
    L.mark("e1", 30, .57, .53, .55)   # e1 leans yes (kalshi): entry ask .42 at decision (mark 0), exit bid .53
    L.event({"id": "e3", "source": "cnbc", "headline": "Bearish", "seen_ts": now - 20})
    L.decision("e3", total_ms=300, action="PASS", reason="weak_signal", venue="kalshi", market_id="MK3",
               market_question="Q3?", p_up=.1, p_down=.8, mid_at_decision=.50, decided_ts=now - 20)
    L.mark("e3", 0, .52, .50, .51)     # leans no: entry = 1 - .50 = .50
    L.mark("e3", 5, .56, .54, .55)     # exit = 1 - .56 = .44
    by = {x["id"]: x for x in c.get("/decisions").json()["decisions"]}
    fees = lambda a, b: .07 * a * (1 - a) + .07 * b * (1 - b)
    assert by["e1"]["move_cents_our_way"] == pytest.approx(15)                       # mid .40 -> .55, before costs
    assert by["e1"]["after_costs_cents"] == pytest.approx((.53 - .42 - fees(.42, .53)) * 100, abs=.01)
    assert by["e1"]["after_costs_horizon_s"] == 30
    assert by["e3"]["move_cents_our_way"] == pytest.approx(-5)
    assert by["e3"]["after_costs_cents"] == pytest.approx((.44 - .50 - fees(.50, .44)) * 100, abs=.01)
    assert by["e4"]["after_costs_cents"] is None                                      # no real mark 0 for e4
    assert by["e1"]["after_costs_cents"] < by["e1"]["move_cents_our_way"]             # costs always make it worse


def test_old_schema_ledger_still_served(old_ledger_path, monkeypatch, tmp_path):
    before = old_ledger_path.read_bytes()
    monkeypatch.setattr(api, "DB_PATH", old_ledger_path)
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")

    async def none(market):
        return None
    monkeypatch.setattr(api, "_book", none)
    api._books.clear(); api._markets.clear()
    c = TestClient(api.app)
    d = c.get("/trades").json()
    assert d["trades"][0]["shadow"] is False and d["trades"][0]["bucket"] is None and d["summary"]["shadow_trades"] == 0
    dd = c.get("/decisions").json()["decisions"]
    assert dd[0]["shadow_action"] is None and dd[0]["shadow_traded"] is False and dd[0]["after_costs_cents"] is None
    assert c.get("/health").json()["x"]["calls_today"] == 0   # tables absent: defaults, no 500
    assert old_ledger_path.read_bytes() == before   # opened read-only: the API never migrates or writes
    from fastlane.ledger import columns
    import sqlite3
    assert "shadow" not in columns(sqlite3.connect(old_ledger_path), "trades")


def test_health_reports_fast_sources(seeded):
    from fastlane.ledger import utc_day
    c, L, _ = seeded
    L.x_spend_add(utc_day(), 0.11, calls=2)
    L.feed_status_set("x", True, {"enabled": True, "budget_hit": False, "in_window": True})
    L.feed_status_set("bsky", True, {"mode": "ws"})
    h = c.get("/health").json()
    assert h["x"]["calls_today"] == 2 and h["x"]["spend_today_usd"] == pytest.approx(0.11)
    assert h["x"]["enabled"] is True and h["x"]["in_window"] is True and h["x"]["budget_hit"] is False
    assert h["bluesky"]["connected"] is True and h["bluesky"]["mode"] == "ws"
    L.db.execute("UPDATE feed_status SET updated_ts = ?", (time.time() - 600,))
    L.db.commit()
    h = c.get("/health").json()
    assert h["bluesky"]["connected"] is False and h["bluesky"]["mode"] == "ws"      # stale heartbeat: engine not running
    assert h["x"]["enabled"] is True and h["x"]["in_window"] is False      # enabled = configured, not fresh


def test_health_x_budget_hit_flag(seeded):
    c, L, _ = seeded
    L.feed_status_set("x", False, {"enabled": True, "budget_hit": True, "in_window": True})
    assert c.get("/health").json()["x"]["budget_hit"] is True


# ---------- v0.4.0: stage timings, /status, /settings, shadow toggle ----------
def test_decisions_carry_stage_timings(seeded):
    c, L, now = seeded
    L.db.execute("UPDATE decisions SET shortlist_ms = 12, book_ms = 34, n_candidates = 5 WHERE event_id = 'e1'")
    by = {x["id"]: x for x in c.get("/decisions").json()["decisions"]}
    assert set(by["e1"]) >= {"shortlist_ms", "book_ms", "n_candidates"}
    assert (by["e1"]["shortlist_ms"], by["e1"]["book_ms"], by["e1"]["n_candidates"]) == (12, 34, 5)
    assert by["e1"]["jev_ms"] == 300            # existing fields untouched
    assert by["e4"]["book_ms"] is None          # unset timings stay null, never raise


def test_status(seeded):
    c, L, now = seeded
    L.tick("MK1", now - 5, .40, .42)
    s = c.get("/status").json()
    assert s["ledger"] is True and s["now"] == pytest.approx(now, abs=5)
    assert s["last_event_ts"] == pytest.approx(now - 30, abs=1)      # e4; synthetic e2 excluded
    assert s["last_decision_ts"] == pytest.approx(now - 30, abs=1)
    assert s["last_trade_ts"] == pytest.approx(now - 30, abs=1)      # shadow trade counts as engine activity
    assert s["last_tick_ts"] == pytest.approx(now - 5, abs=1) and s["ticks_1h"] == 1
    assert s["events_1h"] == 2 and s["decisions_1h"] == 2
    assert [x["source"] for x in s["sources"]] == ["cnbc"] and s["sources"][0]["events_1h"] == 2
    assert s["sources"][0]["last_seen_ts"] == pytest.approx(now - 30, abs=1)


def test_status_counts_only_the_last_hour(seeded):
    c, L, now = seeded
    L.tick("MK1", now - 7200, .40, .42)
    L.event({"id": "old", "source": "reuters", "headline": "Yesterday", "seen_ts": now - 7200})
    s = c.get("/status").json()
    assert s["ticks_1h"] == 0 and s["last_tick_ts"] == pytest.approx(now - 7200, abs=1)
    by = {x["source"]: x for x in s["sources"]}
    assert by["reuters"]["events_1h"] == 0 and by["cnbc"]["events_1h"] == 2
    assert s["sources"][0]["source"] == "cnbc"          # newest first


def test_status_carries_x_and_bluesky_blocks(seeded):
    c, L, now = seeded
    s = c.get("/status").json()
    assert s["x"]["enabled"] is False and s["x"]["budget_usd"] is None and s["bluesky"]["connected"] is False
    L.feed_status_set("x", True, {"enabled": True, "budget_hit": False, "in_window": True, "calls_today": 3,
                                  "spend_today_usd": .18, "budget_usd": 5.0})
    L.feed_status_set("bsky", True, {"mode": "poll"})
    s = c.get("/status").json()
    assert s["x"]["enabled"] is True and s["x"]["budget_usd"] == 5.0 and s["x"]["in_window"] is True
    assert s["bluesky"]["connected"] is True and s["bluesky"]["mode"] == "poll"
    h = c.get("/health").json()
    assert h["x"]["enabled"] is True and h["bluesky"]["mode"] == "poll"     # /health untouched by the repeat


def test_status_x_disabled_row(seeded):
    c, L, _ = seeded
    L.feed_status_set("x", False, {"enabled": False})
    s = c.get("/status").json()
    assert s["x"]["enabled"] is False and s["x"]["budget_usd"] is None


def test_status_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    s = TestClient(api.app).get("/status").json()
    assert s["ledger"] is False and s["last_event_ts"] is None and s["events_1h"] == 0 and s["sources"] == []
    assert s["decisions_1h"] == 0 and s["ticks_1h"] == 0 and s["last_trade_ts"] is None and s["last_tick_ts"] is None
    assert not (tmp_path / "absent.db").exists()


def test_status_on_old_schema_ledger(old_ledger_path, monkeypatch, tmp_path):
    """v0.1.1 file: no feed_status, x_spend, settings, ticks_ts index. Must serve, never 500, never write."""
    monkeypatch.setattr(api, "DB_PATH", old_ledger_path)
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    r = TestClient(api.app).get("/status")
    assert r.status_code == 200
    s = r.json()
    assert s["ledger"] is True and s["last_event_ts"] == 905.0 and s["last_decision_ts"] == 906.0
    assert s["last_trade_ts"] == 906.0 and s["last_tick_ts"] is None
    assert s["events_1h"] == 0 and s["ticks_1h"] == 0
    assert [x["source"] for x in s["sources"]] == ["cnbc"]
    assert s["x"]["enabled"] is False and s["bluesky"]["connected"] is False
    import sqlite3
    names = {r[0] for r in sqlite3.connect(old_ledger_path).execute("SELECT name FROM sqlite_master")}
    assert "settings" not in names and "ticks_ts" not in names         # GET never migrates


def test_health_keeps_ok_and_ledger_keys(seeded):
    c, _, _ = seeded
    h = c.get("/health").json()
    assert h["ok"] is True and h["ledger"] is True      # v0.3.0 added x and bluesky; the originals keep meaning


CTRL = {"X-Fastlane-Control": "1"}


def test_settings_get_env_fallback(seeded, monkeypatch):
    c, _, _ = seeded
    assert c.get("/settings").json() == {"shadow_enabled": True, "source": "env"}
    monkeypatch.setenv("SHADOW_ENABLED", "false")
    assert c.get("/settings").json() == {"shadow_enabled": False, "source": "env"}


def test_shadow_toggle_persists_and_wins_over_env(seeded, monkeypatch):
    c, L, _ = seeded
    monkeypatch.setenv("SHADOW_ENABLED", "false")
    r = c.post("/settings/shadow", json={"enabled": True}, headers=CTRL)
    assert r.status_code == 200 and r.json() == {"shadow_enabled": True, "source": "ledger"}
    assert c.get("/settings").json() == {"shadow_enabled": True, "source": "ledger"}
    assert L.get_setting("shadow_enabled") == "1"                      # visible on the engine's connection
    r = c.post("/settings/shadow", json={"enabled": False}, headers=CTRL)
    assert r.json()["shadow_enabled"] is False and L.get_setting("shadow_enabled") == "0"
    r = c.post("/settings/shadow", json={"enabled": False}, headers={**CTRL, "Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200                                         # charset parameter is accepted


def test_shadow_toggle_rejects_bad_requests(seeded):
    c, _, _ = seeded
    assert c.post("/settings/shadow", json={"enabled": True}).status_code == 403                       # no header
    assert c.post("/settings/shadow", json={"enabled": True}, headers={"X-Fastlane-Control": "yes"}).status_code == 403
    assert c.post("/settings/shadow", content='{"enabled": true}', headers={**CTRL, "Content-Type": "text/plain"}).status_code == 415
    assert c.post("/settings/shadow", content="enabled=true", headers={**CTRL, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert c.post("/settings/shadow", content="{not json", headers={**CTRL, "Content-Type": "application/json"}).status_code == 400
    assert c.post("/settings/shadow", json={"enabled": "yes"}, headers=CTRL).status_code == 400          # not a bool
    assert c.post("/settings/shadow", json={"enabled": 1}, headers=CTRL).status_code == 400              # int is not a bool
    assert c.post("/settings/shadow", json={"enabled": None}, headers=CTRL).status_code == 400
    assert c.post("/settings/shadow", json=[True], headers=CTRL).status_code == 400                      # not an object
    assert c.post("/settings/shadow", json={}, headers=CTRL).status_code == 400
    assert c.post("/settings/shadow", json={"enabled": True, "other": 1}, headers=CTRL).status_code == 400   # extra keys
    assert c.post("/settings/shadow", json={"shadow_signal_threshold": .5}, headers=CTRL).status_code == 400  # only shadow
    assert c.post("/settings/shadow", json={"enabled": True}, headers={**CTRL, "host": "evil.example.net"}).status_code == 400
    assert c.get("/settings").json()["source"] == "env"                 # nothing above wrote anything


def test_shadow_toggle_rejects_large_content_length_first(seeded, monkeypatch):
    from starlette.requests import Request
    c, _, _ = seeded
    reads = []

    async def tripwire(self, *a, **k):
        reads.append(1)
        raise AssertionError("request body was consumed before the 413")

    def tripwire_stream(self):
        reads.append(1)
        raise AssertionError("request body was consumed before the 413")
    monkeypatch.setattr(Request, "body", tripwire)
    monkeypatch.setattr(Request, "stream", tripwire_stream)
    r = c.post("/settings/shadow", content=b"x" * 1000, headers={**CTRL, "Content-Type": "application/json"})
    assert r.status_code == 413 and reads == []
    monkeypatch.undo()
    assert c.get("/settings").json()["source"] == "env"


def test_settings_returns_503_on_transient_db_error(seeded, monkeypatch):
    import sqlite3 as _sq
    c, _, _ = seeded

    def locked(db):
        raise _sq.OperationalError("database is locked")
    monkeypatch.setattr(api, "shadow_enabled_from", locked)
    r = c.get("/settings")
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_shadow_toggle_rejects_large_body(seeded):
    c, _, _ = seeded
    big = '{"enabled": true, "pad": "' + "x" * 400 + '"}'
    r = c.post("/settings/shadow", content=big, headers={**CTRL, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert c.get("/settings").json()["source"] == "env"


def test_shadow_toggle_checks_run_in_order(seeded):
    """Header is checked before content type, content type before body: a bad request leaks nothing about the rest."""
    c, _, _ = seeded
    r = c.post("/settings/shadow", content="x" * 1000, headers={"Content-Type": "text/plain"})
    assert r.status_code == 403
    r = c.post("/settings/shadow", content="x" * 1000, headers={**CTRL, "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_shadow_toggle_has_no_cors_and_preflight_fails(seeded):
    c, _, _ = seeded
    r = c.options("/settings/shadow", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "x-fastlane-control,content-type"})
    assert r.status_code in (400, 404, 405) and "access-control-allow-origin" not in r.headers
    r = c.post("/settings/shadow", json={"enabled": True}, headers={**CTRL, "Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_shadow_toggle_without_ledger_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "absent.db")
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    c = TestClient(api.app)
    r = c.post("/settings/shadow", json={"enabled": False}, headers=CTRL)
    assert r.status_code == 503 and "no ledger" in r.json()["detail"]
    assert not (tmp_path / "absent.db").exists()                        # the API never creates the ledger
    assert c.get("/settings").json() == {"shadow_enabled": True, "source": "env"}


def test_shadow_toggle_on_old_schema_ledger(old_ledger_path, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DB_PATH", old_ledger_path)
    monkeypatch.setattr(api, "CACHE", tmp_path / "none.json")
    c = TestClient(api.app)
    assert c.get("/settings").json()["source"] == "env"                 # no settings table yet: must not 500
    assert c.post("/settings/shadow", json={"enabled": False}, headers=CTRL).status_code == 200
    assert c.get("/settings").json() == {"shadow_enabled": False, "source": "ledger"}
    assert c.get("/trades").status_code == 200                          # read paths unaffected


def test_other_routes_still_reject_post(seeded):
    c, _, _ = seeded
    for path in ("/trades", "/decisions", "/status", "/settings", "/health"):
        assert c.post(path, json={}, headers=CTRL).status_code == 405, path
    for verb in ("put", "delete", "patch"):
        assert c.request(verb.upper(), "/settings/shadow", json={"enabled": True}, headers=CTRL).status_code == 405, verb
    assert c.get("/settings/shadow").status_code == 405
