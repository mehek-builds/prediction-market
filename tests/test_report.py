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
