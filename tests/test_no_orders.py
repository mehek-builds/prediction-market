"""Real orders can only come from one place: fastlane/live.py, one POST to Kalshi's Create Order (V2) endpoint.

Policy: order-capable code exists only in fastlane/live.py, at exactly one call site (`LiveTrader.buy`:
`self.http.post(self.base_url + ORDER_PATH, ...)` signed by `sign_headers("POST", ORDER_PATH)`). Outbound POSTs exist
only in jev_client.py (Jev), x_feed.py (xAI) and that one order call. The API's non-GET routes are exactly
`POST /settings/shadow` and `POST /control/mode`, which write a ledger setting and a mode file and can never place,
size or route an order. Everything else in fastlane/ stays read-only. No cancel, amend, batch, sell-to-close or
Polymarket order code exists.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# fastlane/results/ is gitignored runtime output; the Vercel deploy builds a copy of the package in there.
FILES = sorted(p for p in (ROOT / "fastlane").rglob("*.py") if "results" not in p.relative_to(ROOT / "fastlane").parts)
LIVE = ROOT / "fastlane" / "live.py"

ORDER_PATTERNS = [r"portfolio/orders", r"/orders\b", r"batch_orders", r"place_order", r"create_order",
                  r"cancel_order", r"amend_order"]
NEVER_ANYWHERE = [r"portfolio/orders\b(?!/)", r"batch", r"(?<!immediate_or_)cancel(?!_order_on_pause)", r"amend",
                  r"decrease", r"reduce_only\"?\s*:(?!\s*False)", r"/portfolio/events/orders/",
                  r"[\"']action[\"']"]
POSTERS = {"jev_client.py": "DECISIONS_URL", "x_feed.py": "XAI_RESPONSES_URL"}   # outbound POSTs that cannot trade
POST_LITERAL = r"[\"']POST[\"']|method\s*=\s*[\"']POST"


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


def test_mutating_http_only_in_known_posters():
    for f in FILES:
        text = f.read_text()
        if f.name in POSTERS:
            assert not re.search(POST_LITERAL, text), f.name   # only via .post(
            assert text.count(".post(") == 1 and POSTERS[f.name] in text, f.name
            assert not re.search(r"(?<!queue)\.(put|delete|patch|request)\(", text)   # queue.put is the event queue
            assert not re.search(r"(?<!ws)\.send\(", text), f.name
            if f.name == "jev_client.py":
                assert not re.search(r"kalshi|polymarket", text, re.I)
            else:   # x_feed.py lists the public Polymarket X handle, so check for exchange hosts instead of the word
                assert not re.search(r"kalshi|polymarket\.com|clob\.", text, re.I)
        elif f == LIVE:
            assert text.count(".post(") == 1 and "self.base_url + ORDER_PATH" in text
            # "POST" appears only as the method name inside the request signature for that one order call
            assert re.findall(POST_LITERAL, text) == ['"POST"'] and 'sign_headers("POST", ORDER_PATH)' in text
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
            assert not re.search(r"(?<!ws)\.send\(", text)
        elif f.name == "api.py":   # the two guarded control routes: POST /settings/shadow and POST /control/mode
            assert text.count(".post(") == 2
            assert re.findall(r'^@app\.post\("([^"]+)"\)$', text, re.M) == ["/settings/shadow", "/control/mode"]
            assert not re.search(r"\.(put|delete|patch|request)\(", text)
            assert not re.search(r"(?<!ws)\.send\(", text)
            assert not re.search(POST_LITERAL, text)
        else:
            # @app.post decorators live in api.py and are checked above
            assert not re.search(r"(?<!queue)(?<!@app)\.(post|put|delete|patch|request)\(", text), f.name
            assert not re.search(r"(?<!ws)\.send\(", text), f.name        # client.send(httpx.Request("POST", ...))
            assert not re.search(POST_LITERAL, text), f.name


def test_xai_posts_only_to_the_responses_endpoint():
    text = (ROOT / "fastlane/x_feed.py").read_text()
    assert 'XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"' in text
    assert re.search(r"\.post\(\s*XAI_RESPONSES_URL", text)


def test_kalshi_module_does_not_import_httpx():
    assert not re.search(r"^\s*(import|from)\s+httpx", (ROOT / "fastlane/kalshi.py").read_text(), re.M)


def test_api_routes_are_get_except_the_two_control_posts():
    text = (ROOT / "fastlane/api.py").read_text()
    routes = re.findall(r'^@app\.(\w+)\("([^"]+)"', text, re.M)
    assert routes and {m for m, _ in routes} <= {"get", "post"}
    assert [p for m, p in routes if m == "post"] == ["/settings/shadow", "/control/mode"]
    assert not re.search(r"@app\.(put|patch|delete|api_route)\(|add_api_route\(", text)


def test_no_legacy_order_fields_or_reduce_only_outside_live_module():
    for f in FILES:
        if f == LIVE:
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            assert not re.search(r"[\"']action[\"']\s*:\s*[\"'](buy|sell)[\"']", line, re.I), f"{f}:{n} legacy order action"
            assert not re.search(r"reduce_only", line), f"{f}:{n} reduce_only belongs in live.py only"


TOOLS = sorted((ROOT / "tools").rglob("*.py"))


def test_tools_have_exactly_one_guarded_order_call():
    """tools/ may hold the demo order check and nothing else that can send an order."""
    assert [t.name for t in TOOLS] == ["demo_no_order_check.py"]
    posts = 0
    for t in TOOLS:
        text = t.read_text()
        code = re.sub(r'"""[\s\S]*?"""', "", text)
        posts += code.count(".post(")
        assert not re.search(r"\.(put|delete|patch|request)\(", code) and not re.search(r"(?<!ws)\.send\(", code), t.name
        assert not re.search(POST_LITERAL, code.replace('sign_headers("POST", live.ORDER_PATH)', "")), t.name
        assert "httpx.post(base + live.ORDER_PATH" in code
        assert "base != KALSHI_DEMO_BASE" in code            # the demo-only guard
        assert not re.search(r"polymarket|clob|batch|amend|decrease|api\.elections\.kalshi\.com", code, re.I), t.name
    assert posts == 1
