from fastlane import report


def test_report_without_ledger_is_friendly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "DB_PATH", tmp_path / "nope" / "ledger.db")
    report.main(None)
    assert "No ledger yet" in capsys.readouterr().out
