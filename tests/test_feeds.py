import asyncio
import time

from fastlane import feeds
from fastlane.feeds import FEEDS, FeedHub, _clean


def _entry(title, summary="", link="u"):
    return {"title": title, "summary": summary, "link": link, "published_parsed": time.gmtime(1_700_000_000)}


def test_edgar_with_items():
    ev = FeedHub._edgar("edgar_8k", _entry(
        "8-K - AVIENT CORP (0001122976) (Filer)",
        "Item 2.02: Results of Operations and Financial Condition<br>Item 9.01: Financial Statements and Exhibits"))
    assert ev["headline"] == "AVIENT CORP files 8-K: Results of Operations and Financial Condition; Financial Statements and Exhibits"
    assert ev["published_ts"] == 1_700_000_000.0 and ev["url"] == "u"


def test_edgar_no_items_and_amendment():
    assert FeedHub._edgar("e", _entry("8-K - AVIENT CORP (0001122976) (Filer)"))["headline"] == "AVIENT CORP files 8-K"
    assert FeedHub._edgar("e", _entry("8-K/A - AVIENT CORP (0001122976) (Filer)"))["headline"] == "AVIENT CORP files 8-K"


def test_clean_and_default_truncation():
    assert _clean("<p>Fed  &amp; \n  <b>cuts</b></p>") == "Fed & cuts"
    ev = FeedHub._default("src", {"title": "T", "summary": "x" * 600, "link": "l"})
    assert len(ev["summary"]) == 500 and ev["published_ts"] is None


def test_specs_without_sec_ua(capsys):
    hub = FeedHub(asyncio.Queue(), {})
    assert len(hub.specs()) == len(FEEDS)
    hub.specs()
    out = capsys.readouterr().out
    assert out.count("SEC_USER_AGENT not set") == 1  # printed once only


def test_specs_with_sec_ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Name test@invalid.test")
    specs = FeedHub(asyncio.Queue(), {}).specs()
    assert len(specs) == len(FEEDS) + 1
    name, url, every, headers, parse = specs[-1]
    assert name == "edgar_8k" and headers == {"User-Agent": "Test Name test@invalid.test"} and parse is not None


def test_fetch_snapshot_dedupes_and_skips_failures(monkeypatch):
    rss = b"""<rss version="2.0"><channel><item><title>Same Headline</title></item>
    <item><title></title></item></channel></rss>"""

    class Resp:
        content = rss

    class Client:
        async def get(self, url, **kw):
            if "bbc" in url:
                raise RuntimeError("down")
            return Resp()

    out = asyncio.run(feeds.fetch_snapshot(Client()))
    assert [i["headline"] for i in out] == ["Same Headline"]
