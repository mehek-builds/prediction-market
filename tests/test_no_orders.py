import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "fastlane").rglob("*.py"))

ORDER_PATTERNS = [r"portfolio/orders", r"/orders\b", r"batch_orders", r"place_order", r"create_order",
                  r"cancel_order", r"amend_order"]


def test_files_found():
    assert len(FILES) >= 10


def test_no_order_endpoints():
    for f in FILES:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for pat in ORDER_PATTERNS:
                assert not re.search(pat, line, re.I), f"{f}:{n} matches {pat}"
            if re.search(r"polymarket|clob", line, re.I):
                assert not re.search(r"/order\b", line, re.I), f"{f}:{n} polymarket order endpoint"


def test_mutating_http_only_in_jev_client():
    for f in FILES:
        text = f.read_text()
        if f.name == "jev_client.py":
            assert text.count(".post(") == 1 and "DECISIONS_URL" in text
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
            assert not re.search(r"kalshi|polymarket", text, re.I)
        else:
            assert not re.search(r"(?<!queue)\.(post|put|delete|patch|request)\(", text), f.name


def test_kalshi_module_does_not_import_httpx():
    assert not re.search(r"^\s*(import|from)\s+httpx", (ROOT / "fastlane/kalshi.py").read_text(), re.M)


def test_api_routes_are_get_only():
    decorators = re.findall(r"^@app\.(\w+)\(", (ROOT / "fastlane/api.py").read_text(), re.M)
    assert decorators and set(decorators) == {"get"}
