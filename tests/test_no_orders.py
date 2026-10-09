"""Real orders can only come from one place: fastlane/live.py, one POST to Kalshi's Create Order (V2) endpoint.

Everything else in fastlane/ stays read-only. No cancel, amend, batch, sell-to-close or Polymarket order code exists.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# fastlane/results/ is gitignored runtime output; the Vercel deploy builds a copy of the package in there.
FILES = sorted(p for p in (ROOT / "fastlane").rglob("*.py") if "results" not in p.relative_to(ROOT / "fastlane").parts)
LIVE = ROOT / "fastlane" / "live.py"

ORDER_PATTERNS = [r"portfolio/orders", r"/orders\b", r"batch_orders", r"place_order", r"create_order",
                  r"cancel_order", r"amend_order"]
NEVER_ANYWHERE = [r"portfolio/orders\b(?!/)", r"batch", r"(?<!immediate_or_)cancel(?!_order_on_pause)", r"amend", r"decrease", r"reduce_only\"?\s*:\s*True",
                  r"/portfolio/events/orders/"]


def test_files_found():
    assert len(FILES) >= 10
    assert not any("results" in p.parts for p in FILES)


def test_order_endpoints_only_in_live_module():
    for f in FILES:
        if f == LIVE:
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for pat in ORDER_PATTERNS + [r"events/orders"]:
                assert not re.search(pat, line, re.I), f"{f}:{n} matches {pat}"
            if re.search(r"polymarket|clob", line, re.I):
                assert not re.search(r"/order\b", line, re.I), f"{f}:{n} polymarket order endpoint"


def test_live_module_only_creates_orders():
    code = "\n".join(l for l in LIVE.read_text().splitlines() if not l.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)  # docstrings may describe what is not done
    assert code.count("/portfolio/events/orders") == 1
    for pat in NEVER_ANYWHERE:
        assert not re.search(pat, code, re.I), pat
    assert not re.search(r"polymarket|clob", code, re.I)
    assert '"time_in_force": "immediate_or_cancel"' in code   # nothing ever rests on the book


def test_mutating_http_only_in_jev_client_and_live():
    for f in FILES:
        text = f.read_text()
        if f.name == "jev_client.py":
            assert text.count(".post(") == 1 and "DECISIONS_URL" in text
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
            assert not re.search(r"kalshi|polymarket", text, re.I)
        elif f == LIVE:
            assert text.count(".post(") == 1 and "self.base_url + ORDER_PATH" in text
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
        else:
            assert not re.search(r"(?<!queue)(?<!@app)\.(post|put|delete|patch|request)\(", text), f.name  # @app.post is a route


def test_kalshi_module_does_not_import_httpx():
    assert not re.search(r"^\s*(import|from)\s+httpx", (ROOT / "fastlane/kalshi.py").read_text(), re.M)


def test_api_routes_are_get_only_except_the_mode_switch():
    src = (ROOT / "fastlane/api.py").read_text()
    decorators = re.findall(r"^@app\.(\w+)\(\"([^\"]+)\"", src, re.M)
    assert decorators
    assert {m for m, _ in decorators} <= {"get", "post"}
    assert [p for m, p in decorators if m == "post"] == ["/control/mode"]
