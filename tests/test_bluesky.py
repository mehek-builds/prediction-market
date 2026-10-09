import asyncio
import json

import orjson
import pytest

from fastlane import bluesky
from fastlane.bluesky import (DEFAULT_HANDLES, JETSTREAM_URL, BlueskyFeed, bsky_settings, iso_ts, jetstream_url,
                              parse_author_feed, parse_jetstream)


def commit(did="did:plc:reuters", text="Breaking", created="2025-10-12T14:00:00.123Z", **extra):
    rec = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created, **extra}
    return {"did": did, "time_us": 1_760_277_600_000_000, "kind": "commit",
            "commit": {"rev": "r", "operation": "create", "collection": "app.bsky.feed.post", "rkey": "3kabc",
                       "record": rec, "cid": "c"}}


DIDS = {"did:plc:reuters": "reuters.com"}
SEEN = 1_760_277_605.0


def test_jetstream_post():
    ev = parse_jetstream(commit(), DIDS, SEEN)
    assert ev["source"] == "bsky:reuters.com" and ev["headline"] == "Breaking" and ev["summary"] == ""
    assert ev["url"] == "https://bsky.app/profile/reuters.com/post/3kabc" and ev["id"].startswith("bsky-")
    assert ev["published_ts"] == pytest.approx(iso_ts("2025-10-12T14:00:00.123Z")) and ev["seen_ts"] == SEEN


def test_jetstream_skips_reply_repost_delete_foreign_empty():
    assert parse_jetstream(commit(reply={"root": {}, "parent": {}}), DIDS, SEEN) is None
    m = commit(); m["commit"]["collection"] = "app.bsky.feed.repost"
    m["commit"]["record"] = {"$type": "app.bsky.feed.repost", "subject": {}}
    assert parse_jetstream(m, DIDS, SEEN) is None
    m = commit(); m["commit"]["operation"] = "delete"; m["commit"].pop("record")
    assert parse_jetstream(m, DIDS, SEEN) is None
    m = commit(); m["commit"]["operation"] = "update"
    assert parse_jetstream(m, DIDS, SEEN) is None
    assert parse_jetstream(commit(did="did:plc:other"), DIDS, SEEN) is None
    assert parse_jetstream(commit(text=" "), DIDS, SEEN) is None
    assert parse_jetstream({"kind": "identity", "did": "did:plc:reuters"}, DIDS, SEEN) is None
    assert parse_jetstream({"kind": "account", "did": "did:plc:reuters"}, DIDS, SEEN) is None
    assert parse_jetstream("garbage", DIDS, SEEN) is None


def test_created_at_clamped_and_fallback():
    ev = parse_jetstream(commit(created="2030-01-01T00:00:00Z"), DIDS, SEEN)
    assert ev["published_ts"] == SEEN                                   # future clock: clamped to receipt
    ev = parse_jetstream(commit(created="garbage"), DIDS, SEEN)
    assert ev["published_ts"] == pytest.approx(1_760_277_600.0)         # time_us fallback
    assert iso_ts("2025-10-12T14:00:00.1234567+00:00") == pytest.approx(iso_ts("2025-10-12T14:00:00.123456Z"))
    assert iso_ts(None) is None and iso_ts("") is None and iso_ts("nope") is None


def test_headline_cleaned_and_capped():
    ev = parse_jetstream(commit(text="<b>" + "z" * 400 + "</b>"), DIDS, SEEN)
    assert len(ev["headline"]) == 280 and "<" not in ev["headline"]


def test_author_feed_parsing():
    def item(uri, text, **kw):
        return {"post": {"uri": uri, "cid": "c", "author": {"did": "did:plc:reuters", "handle": "reuters.com"},
                         "record": {"text": text, "createdAt": "2025-10-12T14:00:00Z"},
                         "indexedAt": "2025-10-12T14:00:01Z"}, **kw}
    payload = {"feed": [item("at://did:plc:reuters/app.bsky.feed.post/a1", "own post"),
                        item("at://did:plc:other/app.bsky.feed.post/b2", "reposted",
                             reason={"$type": "app.bsky.feed.defs#reasonRepost"}),
                        item("at://did:plc:reuters/app.bsky.feed.post/c3", "a reply", reply={"root": {}, "parent": {}}),
                        {"post": {"uri": "at://x/y/z", "author": {"did": "did:plc:imposter"},
                                  "record": {"text": "other author"}}},
                        {"broken": True}]}
    out = parse_author_feed(payload, "reuters.com", "did:plc:reuters", SEEN)
    assert [e["headline"] for e in out] == ["own post"] and out[0]["url"].endswith("/post/a1")


def test_author_feed_published_falls_back_to_indexed_at():
    p = {"feed": [{"post": {"uri": "at://did:plc:reuters/app.bsky.feed.post/a1",
                            "author": {"did": "did:plc:reuters"}, "record": {"text": "t", "createdAt": "bad"},
                            "indexedAt": "2025-10-12T14:00:01Z"}}]}
    ev = parse_author_feed(p, "reuters.com", "did:plc:reuters", SEEN)[0]
    assert ev["published_ts"] == pytest.approx(iso_ts("2025-10-12T14:00:01Z"))


def test_jetstream_url_and_settings(monkeypatch):
    assert jetstream_url(["did:plc:a", "did:plc:b"]) == (JETSTREAM_URL + "?wantedCollections=app.bsky.feed.post"
                                                        "&wantedDids=did%3Aplc%3Aa&wantedDids=did%3Aplc%3Ab")
    assert bsky_settings().handles == DEFAULT_HANDLES and bsky_settings().enabled is True
    monkeypatch.setenv("BSKY_HANDLES", "Reuters.com, apnews.com")
    assert bsky_settings().handles == ("reuters.com", "apnews.com")
    monkeypatch.setenv("BSKY_ENABLED", "false")
    assert bsky_settings().enabled is False
    monkeypatch.setenv("BSKY_HANDLES", "not a handle")
    with pytest.raises(ValueError):
        bsky_settings()


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeClient:
    """get() dispatches on a URL substring; feeds is a list of payloads served in order per getAuthorFeed call."""

    def __init__(self, dids=None, feeds=()):
        self.dids = dids or {"reuters.com": "did:plc:reuters"}
        self.feeds, self.urls = list(feeds), []

    async def get(self, url, **kw):
        self.urls.append(url)
        if "resolveHandle" in url:
            h = url.split("handle=")[1]
            if h not in self.dids:
                raise OSError("dns")
            return FakeResp(200, {"did": self.dids[h]})
        if "getAuthorFeed" in url:
            body = self.feeds.pop(0) if len(self.feeds) > 1 else (self.feeds[0] if self.feeds else {"feed": []})
            return FakeResp(200, body)
        return FakeResp(404, {})

    async def aclose(self):
        pass


def feed_item(rkey, text):
    return {"post": {"uri": f"at://did:plc:reuters/app.bsky.feed.post/{rkey}", "author": {"did": "did:plc:reuters"},
                     "record": {"text": text, "createdAt": "2025-10-12T14:00:00Z"}, "indexedAt": "2025-10-12T14:00:01Z"}}


def make_feed(ledger, monkeypatch, client, handles="reuters.com", **kw):
    monkeypatch.setenv("BSKY_HANDLES", handles)
    q, stats = asyncio.Queue(), {}
    return BlueskyFeed(q, stats, ledger, client=client, now=lambda: SEEN, **kw), q, stats


def test_poll_fallback_first_poll_is_backlog(tmp_ledger, monkeypatch):
    client = FakeClient(feeds=[{"feed": [feed_item("a1", "old post")]},
                               {"feed": [feed_item("b2", "new post"), feed_item("a1", "old post")]}])
    feed, q, stats = make_feed(tmp_ledger, monkeypatch, client)
    asyncio.run(feed.resolve())
    assert feed.dids == {"did:plc:reuters": "reuters.com"}
    assert asyncio.run(feed.poll_once("did:plc:reuters", "reuters.com")) == 0 and q.empty()
    assert asyncio.run(feed.poll_once("did:plc:reuters", "reuters.com")) == 1
    ev = q.get_nowait()
    assert ev["source"] == "bsky:reuters.com" and ev["headline"] == "new post"
    assert stats["bsky"]["polls"] == 2 and stats["bsky"]["new"] == 1


def test_poll_skips_posts_already_seen_from_socket(tmp_ledger, monkeypatch):
    client = FakeClient(feeds=[{"feed": []}, {"feed": [feed_item("a1", "from socket")]}])
    feed, q, _ = make_feed(tmp_ledger, monkeypatch, client)
    asyncio.run(feed.resolve())
    asyncio.run(feed.poll_once("did:plc:reuters", "reuters.com"))      # backlog poll
    ev = parse_author_feed({"feed": [feed_item("a1", "from socket")]}, "reuters.com", "did:plc:reuters", SEEN)[0]
    feed._remember(ev["id"])
    assert asyncio.run(feed.poll_once("did:plc:reuters", "reuters.com")) == 0 and q.empty()


def test_poll_http_error_counts_and_returns_zero(tmp_ledger, monkeypatch):
    class Bad(FakeClient):
        async def get(self, url, **kw):
            return FakeResp(503, {})
    feed, q, stats = make_feed(tmp_ledger, monkeypatch, Bad())
    assert asyncio.run(feed.poll_once("did:plc:reuters", "reuters.com")) == 0
    assert stats["bsky"]["errors"] == 1 and q.empty()


def test_resolve_handles(tmp_ledger, monkeypatch, capsys):
    client = FakeClient(dids={"reuters.com": "did:plc:reuters"})
    feed, _, _ = make_feed(tmp_ledger, monkeypatch, client, handles="reuters.com,apnews.com")
    out = asyncio.run(feed.resolve())
    assert out == {"did:plc:reuters": "reuters.com"}
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1 and "apnews.com" in printed[0]


def test_disabled_run_returns(monkeypatch, tmp_ledger):
    monkeypatch.setenv("BSKY_ENABLED", "false")

    def boom(*a, **k):
        raise AssertionError("must not connect")
    feed = BlueskyFeed(asyncio.Queue(), {}, tmp_ledger, ws_connect=boom)
    assert asyncio.run(asyncio.wait_for(feed.run(), 2)) is None


def test_socket_failure_falls_back_to_polling(tmp_ledger, monkeypatch):
    monkeypatch.setattr(bluesky, "FALLBACK_FOR_S", 0.05)
    monkeypatch.setattr(bluesky, "POLL_S", 0.01)

    def refuse(*a, **k):
        raise OSError("refused")
    q, stats = asyncio.Queue(), {}
    monkeypatch.setenv("BSKY_HANDLES", "reuters.com")
    import time
    feed = BlueskyFeed(q, stats, tmp_ledger, client=FakeClient(), ws_connect=refuse, now=time.time)

    async def go():
        try:
            await asyncio.wait_for(feed.run(), 0.3)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    assert stats["bsky"]["mode"] == "poll" and stats["bsky"]["reconnects"] >= 1
    st = tmp_ledger.feed_status("bsky")
    assert st["connected"] is False and st["info"] == {"mode": "poll"}


class FakeWS:
    def __init__(self, msgs):
        self.msgs = msgs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for m in self.msgs:
                yield m
            await asyncio.sleep(10)
        return gen()


def test_socket_messages_are_emitted_deduped_and_filtered(tmp_ledger, monkeypatch):
    msgs = [orjson.dumps(commit()), orjson.dumps(commit()),                   # duplicate delivery
            orjson.dumps(commit(reply={"root": {}, "parent": {}})), b"not json",
            orjson.dumps(commit(did="did:plc:other"))]
    q, stats = asyncio.Queue(), {}
    monkeypatch.setenv("BSKY_HANDLES", "reuters.com")
    feed = BlueskyFeed(q, stats, tmp_ledger, client=FakeClient(), ws_connect=lambda *a, **k: FakeWS(msgs),
                       now=lambda: SEEN)

    async def go():
        try:
            await asyncio.wait_for(feed.run(), 0.3)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    assert q.qsize() == 1 and stats["bsky"]["new"] == 1 and stats["bsky"]["mode"] == "ws"
    assert stats["bsky"]["polls"] == 5
    assert tmp_ledger.feed_status("bsky")["connected"] is True


def test_rkey_validated():
    assert parse_jetstream(commit(), DIDS, SEEN) is not None
    for bad in ("../x", "a/b", "a b", "", "x" * 513, "..", "a?b=c"):
        m = commit()
        m["commit"]["rkey"] = bad
        assert parse_jetstream(m, DIDS, SEEN) is None, bad
