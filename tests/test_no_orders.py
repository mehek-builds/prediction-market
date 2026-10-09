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


POSTERS = {"jev_client.py": "DECISIONS_URL", "x_feed.py": "XAI_RESPONSES_URL"}   # the only two outbound POSTs, both paper


def test_mutating_http_only_in_jev_and_xai_clients():
    for f in FILES:
        text = f.read_text()
        if f.name in POSTERS:
            assert not re.search(r"[\"']POST[\"']|method\s*=\s*[\"']POST", text), f.name   # only via .post(
            assert text.count(".post(") == 1 and POSTERS[f.name] in text, f.name
            assert not re.search(r"(?<!queue)\.(put|delete|patch|request)\(", text)   # queue.put is the event queue
            assert not re.search(r"(?<!ws)\.send\(", text), f.name
            if f.name == "jev_client.py":
                assert not re.search(r"kalshi|polymarket", text, re.I)
            else:   # x_feed.py lists the public Polymarket X handle, so check for exchange hosts instead of the word
                assert not re.search(r"kalshi|polymarket\.com|clob\.", text, re.I)
        elif f.name == "api.py":   # the one control route: POST /settings/shadow (paper-only shadow toggle)
            assert text.count(".post(") == 1 and re.search(r'^@app\.post\("/settings/shadow"\)$', text, re.M)
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
            assert not re.search(r"(?<!ws)\.send\(", text)
            assert not re.search(r"[\"']POST[\"']|method\s*=\s*[\"']POST", text)
        else:
            assert not re.search(r"(?<!queue)\.(post|put|delete|patch|request)\(", text), f.name
            assert not re.search(r"(?<!ws)\.send\(", text), f.name        # client.send(httpx.Request("POST", ...))
            assert not re.search(r"[\"']POST[\"']|method\s*=\s*[\"']POST", text), f.name


def test_xai_posts_only_to_the_responses_endpoint():
    text = (ROOT / "fastlane/x_feed.py").read_text()
    assert 'XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"' in text
    assert re.search(r"\.post\(\s*XAI_RESPONSES_URL", text)


def test_kalshi_module_does_not_import_httpx():
    assert not re.search(r"^\s*(import|from)\s+httpx", (ROOT / "fastlane/kalshi.py").read_text(), re.M)


def test_api_routes_are_get_except_shadow_toggle():
    text = (ROOT / "fastlane/api.py").read_text()
    routes = re.findall(r'^@app\.(\w+)\("([^"]+)"', text, re.M)
    assert routes and all(m == "get" for m, p in routes if p != "/settings/shadow")
    assert [p for m, p in routes if m != "get"] == ["/settings/shadow"]
    assert [m for m, p in routes if p == "/settings/shadow"] == ["post"]
