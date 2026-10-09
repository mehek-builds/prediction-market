"""Static checks on the single-file dashboard: no injection sinks, no external requests, guarded write, a11y, AA contrast."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "fastlane/static/index.html").read_text()
ALLOWED_PATHS = {"/trades", "/decisions", "/status", "/settings", "/settings/shadow"}


def test_no_html_injection_apis():        # textContent only; headlines are third-party text
    for bad in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]:
        assert bad not in HTML, bad


def test_no_external_requests():
    assert not re.search(r"""(src|href)\s*=\s*["']\s*(https?:)?//""", HTML)
    assert "@import" not in HTML and not re.search(r"""url\(\s*['"]?\s*(https?:)?//""", HTML)
    assert not re.search(r"<script[^>]+src=", HTML) and not re.search(r"<link[^>]+stylesheet", HTML)
    assert not re.search(r"""fetch\(\s*["'](?!/)""", HTML)                 # same-origin absolute paths only
    assert not re.search(r"""(XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)""", HTML)


def test_every_fetch_path_is_allow_listed():
    direct = re.findall(r"""fetch\(\s*["'](/[A-Za-z0-9/_-]*)""", HTML)
    helper = re.findall(r"""getJson\(\s*["'](/[A-Za-z0-9/_-]*)""", HTML)
    assert direct and helper                                               # both call shapes are actually used
    assert set(direct) | set(helper) <= ALLOWED_PATHS
    assert {"/trades", "/decisions", "/status", "/settings"} <= set(helper)
    # the helper itself must only fetch the path it is given (no string concatenation onto a foreign origin)
    body = re.search(r"async function getJson\(path\)\s*\{(.*?)\n\}", HTML, re.S).group(1)
    assert re.search(r"fetch\(\s*path\s*,", body)
    # every getJson call site passes a literal path (a computed path could escape the allow-list)
    calls = re.findall(r"getJson\(([^)]*)\)", HTML)
    assert all(re.match(r"""\s*["']/""", c) for c in calls if "path" not in c)


def test_links_are_guarded():
    assert "safeUrl" in HTML and 'rel: "noopener noreferrer"' in HTML and 'target: "_blank"' in HTML


def test_only_one_write_call_and_it_is_guarded():
    posts = re.findall(r"""method:\s*["']POST["']""", HTML)
    assert len(posts) == 1 and HTML.count('"X-Fastlane-Control"') == 1 and HTML.count("/settings/shadow") == 1
    assert not re.search(r"""method:\s*["'](PUT|DELETE|PATCH)["']""", HTML)


def test_theme_and_a11y_hooks():
    for s in ["data-theme", "prefers-color-scheme", "prefers-reduced-motion", "localStorage", "focus-visible",
              'id="cmd"', 'id="theme"', 'id="st-shadow"', "tabular-nums", "America/New_York", "aria-sort"]:
        assert s in HTML, s
    assert chr(0x2014) not in HTML


def test_local_storage_only_inside_try():
    assert re.search(r"try\s*\{[^}]*localStorage", HTML)
    assert HTML.count("localStorage.") == len(re.findall(r"try\s*\{[^}]*localStorage\.", HTML))


def test_reduced_motion_and_focus_rules():
    rm = re.search(r"@media \(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}?", HTML, re.S)
    assert rm and "animation: none" in rm.group(1) and "transition: none" in rm.group(1)
    assert re.search(r":focus-visible\s*\{[^}]*outline:\s*2px solid var\(--amber\)", HTML)
    # the flash animation is only declared for users who did not ask for reduced motion
    assert re.search(r"prefers-reduced-motion:\s*no-preference", HTML)


def test_paper_badge_is_persistent():
    assert ">PAPER<" in HTML and "<title>Fast lane" in HTML
    badge = re.search(r"""<span class="badge paper"[^>]*>PAPER</span>""", HTML)
    assert badge
    # no media query may hide it
    for blk in re.findall(r"@media[^{]*\{(.*?)\n\}", HTML, re.S):
        assert not re.search(r"\.badge[^{]*\{[^}]*display:\s*none", blk)
        assert not re.search(r"\.paper[^{]*\{[^}]*display:\s*none", blk)


def _tokens(theme):                       # {"--bg": "#000000", ...} from the :root[data-theme="..."] block
    block = re.search(rf':root\[data-theme="{theme}"\]\s*\{{(.*?)\}}', HTML, re.S).group(1)
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))


def _lum(h):
    def c(v):
        v /= 255
        return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return .2126 * c(r) + .7152 * c(g) + .0722 * c(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + .05) / (lb + .05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_tokens_meet_aa(theme):
    t = _tokens(theme)
    assert {"--bg", "--fg", "--dim", "--rule", "--amber", "--up", "--down", "--cool"} <= set(t)
    for name in ("--fg", "--dim", "--amber", "--up", "--down", "--cool"):
        assert _contrast(t[name], t["--bg"]) >= 4.5, (theme, name)


def test_same_token_names_in_both_themes():
    assert set(_tokens("dark")) == set(_tokens("light"))


def test_contrast_helper_sanity():
    assert _contrast("#000000", "#FFFFFF") == pytest.approx(21)
    assert _contrast("#777777", "#808080") < 4.5


def test_command_caps_expose_aria_pressed():
    body = re.search(r"function renderCaps\(\)\s*\{(.*?)\n\}\n", HTML, re.S).group(1)
    assert 'indexOf("book:")' in body and 'indexOf("view:")' in body
    assert len(re.findall(r'setAttribute\("aria-pressed"', body)) >= 5     # H, book, view, theme cap, help, #theme
    assert 'c === "theme:toggle"' in body


def test_notice_only_rewritten_when_content_changes():
    body = re.search(r"function renderNotice\(\)\s*\{(.*?)\n\}\n", HTML, re.S).group(1)
    assert "noticeKey" in body and body.index("noticeKey") < body.index("replaceChildren")
    assert "return;" in body.split("replaceChildren")[0]


def test_focus_kept_on_headline_links():
    assert 'data-focus": "n" + d.id' in HTML and 'data-focus": "c" + t.id' in HTML
    body = re.search(r"function renderKeepFocus\(\)\s*\{(.*?)\n\}\n", HTML, re.S).group(1)
    assert 'getAttribute("data-focus")' in body and "querySelectorAll" in body


def test_shadow_cap_uses_aria_disabled_not_disabled_property():
    assert "cap.disabled" not in HTML
    assert 'aria-disabled' in HTML


def test_arrow_nav_ignores_modified_keys():
    body = re.search(r"function arrowNav\(root\)\s*\{(.*?)\n\}", HTML, re.S).group(1)
    guard = body.index("e.altKey || e.metaKey || e.ctrlKey || e.shiftKey")
    assert guard < body.index("preventDefault")
    assert "return" in body[guard:guard + 120]


def test_news_rows_stack_on_phones_without_horizontal_overflow():
    css = re.search(r"@media \(max-width: 480px\) \{\s*#news-table, #news-table tbody(.*?)\n\}\n", HTML, re.S).group(1)
    assert "display: flex" in css and "flex-wrap: wrap" in css          # rows are blocks, not table rows
    assert "-webkit-line-clamp: 2" in css and "text-overflow: ellipsis" in css
    assert "nth-child(4)" in css and "nth-child(5)" in css              # market line shown, chips wrap
    assert "min-width: 0" in css
