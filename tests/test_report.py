from fastlane import report


def test_report_without_ledger_is_friendly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "nope" / "ledger.db")
    report.main(None)
    assert "No ledger yet" in capsys.readouterr().out


def test_shadow_section(tmp_ledger, tmp_path, monkeypatch, capsys):
    import time
    now = time.time(); L = tmp_ledger
    L.event({"id": "e1", "source": "cnbc", "headline": "Real", "seen_ts": now - 100, "published_ts": now - 110})
    L.event({"id": "e2", "source": "cnbc", "headline": "Weak", "seen_ts": now - 90, "published_ts": now - 100})
    L.event({"id": "e3", "source": "cnbc", "headline": "Lean", "seen_ts": now - 80, "published_ts": now - 90})
    L.decision("e1", action="BUY_YES", reason="signal_yes", venue="kalshi", market_id="MK1", decided_ts=now - 100)
    L.decision("e2", action="PASS", reason="weak_signal", venue="kalshi", market_id="MK2", decided_ts=now - 90)
    L.decision("e3", action="PASS", reason="lean_not_decisive", venue="kalshi", market_id="MK3", decided_ts=now - 80)
    L.shadow_decision("e2", "BUY_YES", "signal_yes", "MK2"); L.shadow_decision("e3", "BUY_YES", "signal_yes", "MK3")
    base = dict(venue="kalshi", side="yes", contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=0)
    L.trade(event_id="e1", opened_ts=now - 100, market_id="MK1", market_question="Q1", shadow=0, signal_strength=.9, **base)
    L.trade(event_id="e2", opened_ts=now - 90, market_id="MK2", market_question="Q2", shadow=1, signal_strength=.65, **base)
    L.trade(event_id="e3", opened_ts=now - 80, market_id="MK3", market_question="Q3", shadow=1, signal_strength=.95, **base)
    L.mark("e1", 30, .47, .45, .46)          # real: +4.5c after fee
    L.mark("shadow:e2", 30, .37, .35, .36)   # shadow weak: -5.5c
    L.mark("shadow:e3", 30, .52, .50, .51)   # shadow lean: +9.5c
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    out = capsys.readouterr().out
    assert "Shadow vs real" in out and "Paper trades: 1" in out
    assert "+4.5c" in out and "-5.5c" in out and "+9.5c" in out
    assert "shadow 0.60-0.70" in out and "shadow 0.85+" in out and "0.70-0.85" not in out
    assert "weak_signal 1" in out and "lean_not_decisive 1" in out
    assert "+2.0c" in out   # shadow all: mean of -5.5 and +9.5


def test_report_no_trades_says_so(tmp_ledger, tmp_path, monkeypatch, capsys):
    import time
    now = time.time()
    tmp_ledger.event({"id": "e1", "source": "cnbc", "headline": "x", "seen_ts": now - 10, "published_ts": now - 20})
    tmp_ledger.decision("e1", action="PASS", reason="weak_signal", venue="kalshi", market_id="MK1", decided_ts=now - 10)
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    assert "no real or shadow trades in range" in capsys.readouterr().out


def test_report_on_old_schema_ledger(old_ledger_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "DB_PATH", old_ledger_path)
    report.main(None)   # must not raise on a pre-0.2.0 ledger
    assert "Shadow vs real" in capsys.readouterr().out


def test_source_lag_section_with_fast_sources(tmp_ledger, tmp_path, monkeypatch, capsys):
    import time
    now = time.time(); L = tmp_ledger
    L.event({"id": "x-1", "source": "x:DeItaone", "headline": "wire", "seen_ts": now - 100, "published_ts": now - 140})
    L.event({"id": "bsky-ab", "source": "bsky:reuters.com", "headline": "bsky", "seen_ts": now - 90, "published_ts": now - 92})
    L.event({"id": "f1", "source": "trumpstruth", "headline": "post", "seen_ts": now - 80, "published_ts": now - 380})
    for eid, t in (("x-1", now - 100), ("bsky-ab", now - 90), ("f1", now - 80)):
        L.decision(eid, action="PASS", reason="no_candidates", decided_ts=t, total_ms=5, shortlist_ms=1)
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    out = capsys.readouterr().out
    assert "bsky:reuters.com" in out and "x:DeItaone" in out and "trumpstruth" in out
    assert out.index("bsky:reuters.com") < out.index("x:DeItaone") < out.index("trumpstruth")   # sorted by p50 lag


def _report_ledger(tmp_ledger):
    import time
    now = time.time(); L = tmp_ledger
    for eid, sec in (("r1", 300), ("r2", 200), ("r3", 100)):
        src = "release:KXCPI" if eid == "r3" else "cnbc"
        L.event({"id": eid, "source": src, "headline": eid, "seen_ts": now - sec, "published_ts": now - sec - 5})
    L.release_put(id="cpi-2026-09", kind="cpi", series="KXCPI,KXCPIYOY", period="2026-09", value=0.4, status="done")
    L.decision("r1", action="BUY_YES", reason="signal_yes", venue="kalshi", market_id="MK1", decided_ts=now - 300,
               jev_ms=500, book_ms=10, total_ms=600, shortlist_ms=2, quote_wait_ms=40.0, n_live_quotes=3)
    L.decision("r2", action="PASS", reason="lean_not_decisive", venue="kalshi", market_id="MK2", decided_ts=now - 200,
               total_ms=500, quote_wait_ms=60.0, n_live_quotes=2)
    L.decision("r3", action="BUY_YES", reason="release_yes", venue="kalshi", market_id="MK3", decided_ts=now - 100, total_ms=900)
    base = dict(venue="kalshi", side="yes", contracts=10, avg_price=.40, cost=4.0, fee=.05, best_ask=.40, synthetic=0)
    L.trade(event_id="r1", opened_ts=now - 300, market_id="MK1", market_question="Q1", shadow=0, signal_strength=.9, **base)
    L.trade(event_id="r2", opened_ts=now - 200, market_id="MK2", market_question="Q2", book="starter", shadow=0,
            signal_strength=.92, entry_style="post", **base)
    L.trade(event_id="r3", opened_ts=now - 100, market_id="MK3", market_question="Q3", shadow=0, signal_strength=1.0, **base)
    L.mark("r1", 30, .47, .45, .46)
    L.mark("starter:r2", 30, .52, .50, .51)
    L.mark("r3", 30, .50, .48, .49)
    o = dict(venue="kalshi", side="yes", style="post", limit_price=.40, take_price=.42, requested=10, synthetic=0)
    L.order_place(book="shadow", market_id="MS1", status="filled", filled=10, created_ts=now - 150, closed_ts=now - 140,
                  updated_ts=now - 140, **o)
    L.order_place(book="starter", market_id="MS2", status="post_expired", filled=0, created_ts=now - 400, closed_ts=now - 100,
                  updated_ts=now - 100, **o)
    L.fill_add(order_id=1, ts=now - 140, contracts=10, price=.40, evidence="{}", ask_seen=.40, qty_seen=10)
    return L


def test_report_v050_sections(tmp_ledger, tmp_path, monkeypatch, capsys):
    _report_ledger(tmp_ledger)
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    out = capsys.readouterr().out
    assert "live quote wait after Jev" in out
    assert "Starter book (signal >= 0.90" in out and "trades 1  invested $4.05" in out and "+30s $+0.95" in out
    assert "Working and expired orders" in out and "shadow   filled 1" in out and "starter  post_expired 1" in out
    assert "spread saved on filled orders: avg +2.0c" in out
    assert "Release trades:" in out and "none in range" not in out and "KXCPI" in out.split("Release trades:")[1]


def test_report_starter_trades_are_not_in_the_real_paper_count(tmp_ledger, tmp_path, monkeypatch, capsys):
    _report_ledger(tmp_ledger)
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    assert "Paper trades: 2" in capsys.readouterr().out                  # r1 and r3; the starter trade r2 is separate


def test_report_on_a_ledger_without_orders_or_starter_does_not_raise(tmp_ledger, tmp_path, monkeypatch, capsys):
    import time
    now = time.time()
    tmp_ledger.event({"id": "e1", "source": "cnbc", "headline": "x", "seen_ts": now - 10, "published_ts": now - 20})
    tmp_ledger.decision("e1", action="PASS", reason="weak_signal", venue="kalshi", market_id="MK1", decided_ts=now - 10)
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "ledger.db")
    report.main(None)
    assert "Starter book" in capsys.readouterr().out
