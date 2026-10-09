import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")

BAD_PATHS = [r"(^|/)\.env$", r"\.pem$", r"\.key$", r"\.db(-wal|-shm)?$", r"(^|/)results/", r"^reference/",
             r"\.pipeline/"]
SECRETS = [r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----\s*[A-Za-z0-9+/=]{20,}", r"sk-or-v1-", r"KALSHI-ACCESS-SIGNATURE: [A-Za-z0-9+/=]{20,}"]
EMAIL = re.compile(r"[a-z0-9._-]+@[a-z0-9-]+\.[a-z]+", re.I)
PLACEHOLDER_HOSTS = ("example.com", "invalid.test")
TERMS_FILE = ROOT / ".pipeline" / "scrub-terms.txt"  # gitignored; private terms never live in the published repo


def private_terms() -> list[str]:
    terms: list[str] = []
    if TERMS_FILE.is_file():
        terms += [l.strip() for l in TERMS_FILE.read_text().splitlines() if l.strip()]
    terms += [t.strip() for t in os.environ.get("PRIVATE_SCRUB_TERMS", "").split(",") if t.strip()]
    return terms


def term_regex(terms: list[str]):
    parts = [rf"\b{re.escape(t)}\b" if len(t) <= 4 else re.escape(t) for t in terms]
    return re.compile("|".join(parts), re.I)


def listed() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l and (ROOT / l).is_file()]


def text_of(rel: str):
    data = (ROOT / rel).read_bytes()
    return None if b"\0" in data else data.decode("utf-8", "replace")


def test_no_forbidden_paths():
    files = listed()
    assert files
    bad = [f for f in files if any(re.search(p, f) for p in BAD_PATHS)]
    assert bad == []


def test_no_secrets_in_tracked_files():
    for rel in listed():
        if rel.startswith("tests/"):
            continue
        text = text_of(rel)
        if text is None:
            continue
        for pat in SECRETS:
            assert not re.search(pat, text), f"{rel} matches {pat}"


def _scan_files():
    for rel in listed():
        if rel == "LICENSE":
            continue
        text = text_of(rel)
        if text is not None:
            yield rel, text


def test_no_emails_beyond_placeholders():
    for rel, text in _scan_files():
        if rel.startswith("tests/"):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                assert m.group(0).lower().endswith(PLACEHOLDER_HOSTS), f"{rel}:{n}: {m.group(0)}"


def test_no_private_terms():
    terms = private_terms()
    if not terms:
        pytest.skip("no private scrub terms configured (.pipeline/scrub-terms.txt or PRIVATE_SCRUB_TERMS)")
    rx = term_regex(terms)
    for rel, text in _scan_files():
        if rel == "tests/test_hygiene.py":
            continue
        for n, line in enumerate(text.splitlines(), 1):
            assert not rx.search(line), f"{rel}:{n}: private term"
        assert not rx.search(rel), f"{rel}: private term in path"


def test_no_em_dashes():
    for rel in listed():
        if rel == "LICENSE":
            continue
        text = text_of(rel)
        if text is not None:
            assert chr(0x2014) not in text, rel


def test_gitignore_entries():
    lines = {l.strip() for l in (ROOT / ".gitignore").read_text().splitlines()}
    for want in [".env", "*.pem", "fastlane/results/", "*.db", ".pipeline/", ".venv/", "__pycache__/"]:
        assert want in lines, want


def test_env_example_has_no_values_and_all_vars():
    text = (ROOT / ".env.example").read_text()
    for var in ["OPENROUTER_API_KEY", "JEV_MODEL", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "SEC_USER_AGENT",
                "PAPER_BANKROLL_USD", "PAPER_MAX_TRADE_PCT", "PAPER_DAILY_LOSS_HALT_PCT",
                "SHADOW_ENABLED", "SHADOW_SIGNAL_THRESHOLD", "SHADOW_DECISIVE_MIN", "MAX_SPREAD_CENTS", "COST_TO_ROOM_MAX",
                "XAI_API_KEY", "XAI_X_HANDLES", "XAI_POLL_SECONDS", "XAI_DAILY_BUDGET_USD", "XAI_WINDOW_DAYS",
                "XAI_WINDOW_HOURS", "XAI_WINDOW_TZ", "BSKY_ENABLED", "BSKY_HANDLES", "MIN_ENTRY_PRICE", "MOVE_DENY_RE",
                "LIVE_QUOTE_WAIT_MS", "ENTRY_STYLE", "ENTRY_STYLE_LIVE", "ENTRY_STYLE_SHADOW", "ENTRY_STYLE_STARTER",
                "POST_MAX_WAIT_S", "POST_POLL_S", "POST_MAX_WORKING", "STARTER_ENABLED", "STARTER_SIGNAL_THRESHOLD",
                "STARTER_DECISIVE_MIN", "STARTER_SIZE_USD", "RELEASES_ENABLED", "BLS_API_KEY", "RELEASE_POLL_START_S",
                "RELEASE_POLL_EVERY_S", "RELEASE_POLL_MAX_S", "RELEASE_BLS_DAILY_BUDGET", "RELEASE_MARGIN_CPI_PP",
                "RELEASE_MARGIN_PAYROLLS_K", "RELEASE_MAX_MARKETS_PER_SERIES"]:
        assert re.search(rf"^{var}=", text, re.M), var
    for gone in ["KALSHI_ENV", "X_BEARER_TOKEN", "X_LIST_ID"]:
        assert gone not in text


def test_version_matches_changelog_head():
    import fastlane
    head = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    assert head == fastlane.__version__


def test_version_is_0_5_0():
    import fastlane
    assert fastlane.__version__ == "0.5.0"
    assert re.search(r"^## 0\.5\.0 - \d{4}-\d{2}-\d{2}$", (ROOT / "CHANGELOG.md").read_text(), re.M)


def test_release_calendar_parses_and_keeps_its_todo_when_it_has_empty_kinds():
    import json
    from fastlane import releases
    data = json.loads((ROOT / "fastlane" / "release_calendar.json").read_text())
    assert data["version"] == 1 and isinstance(data["events"], list)
    if not data["events"]:
        assert "todo" in data
    entries = releases.load_calendar()                   # every committed entry validates
    kinds = {e.kind for e in entries}
    if not ({"cpi", "jobs"} <= kinds):
        assert "todo" in data                             # BLS dates unfilled: the instructions stay in the file
    assert set(data["sources"]) == {"cpi", "jobs", "fomc"}
    assert all(e.date for e in entries) and len({e.release_id for e in entries}) == len(entries)


def test_release_fixtures_are_committed_for_every_series():
    fx = ROOT / "tests" / "fixtures" / "release_rules"
    assert sorted(p.stem for p in fx.glob("*.json")) == ["KXCPI", "KXCPIYOY", "KXFED", "KXFEDDECISION", "KXPAYROLLS", "KXU3"]


def test_new_modules_make_no_non_get_requests():
    for name in ("releases.py", "orders.py", "replay.py"):
        text = (ROOT / "fastlane" / name).read_text()
        assert not re.search(r"\.(post|put|delete|patch|request)\(", text), name
        assert not re.search(r"[\"']POST[\"']", text), name
        assert not re.search(r"/orders\b", text), name
        assert not re.search(r"\b(amend|batch)\b", text, re.I), name
