import asyncio, json, time
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastlane import x_feed
from fastlane.ledger import Ledger, utc_day
from fastlane.x_feed import (XFeed, build_body, build_query, call_cost_usd, decode_snowflake, extract_text, in_window,
                             parse_posts, x_settings)

ID_2022 = 1600000000000000000       # ((id >> 22) + 1288834974657) / 1000 = 1670304701.219 (2022-12-06T05:31:41Z)
ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


def payload(posts, ticks=550_000_000):
    text = json.dumps({"posts": posts})
    return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"cost_in_usd_ticks": ticks}}


def snowflake_at(ts):                 # inverse of decode_snowflake, for building test ids
    return (int(ts * 1000) - 1288834974657) << 22


def test_decode_snowflake():
    assert decode_snowflake(ID_2022) == pytest.approx(1670304701.219)
    assert decode_snowflake(snowflake_at(1_700_000_000.5)) == pytest.approx(1_700_000_000.5, abs=1e-3)


def test_settings_off_without_key():
    assert x_settings() is None


def test_settings_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    s = x_settings()
    assert s.handles == x_feed.DEFAULT_HANDLES and s.poll_seconds == 60 and s.budget_usd == 25
    assert s.days == frozenset(range(5)) and (s.start.hour, s.start.minute, s.end.hour, s.end.minute) == (9, 0, 16, 30)
    assert s.tz.key == "America/New_York"
    monkeypatch.setenv("XAI_X_HANDLES", "@Reuters, business ,reuters")        # strip @, dedupe case-insensitively
    assert x_settings().handles == ("Reuters", "business")
    monkeypatch.setenv("XAI_X_HANDLES", "")
    assert x_settings().handles == x_feed.DEFAULT_HANDLES                      # empty -> default
    for bad in ("a,b,c,d,e,f,g,h,i,j,k", "bad handle", "x" * 16):
        monkeypatch.setenv("XAI_X_HANDLES", bad)
        with pytest.raises(ValueError):
            x_settings()
    monkeypatch.setenv("XAI_X_HANDLES", "Reuters")
    for var, bad in (("XAI_POLL_SECONDS", "5"), ("XAI_DAILY_BUDGET_USD", "0"), ("XAI_WINDOW_DAYS", "Funday"),
                     ("XAI_WINDOW_HOURS", "16:30-09:00"), ("XAI_WINDOW_TZ", "Mars/Olympus")):
        monkeypatch.setenv(var, bad)
        with pytest.raises(ValueError):
            x_settings()
        monkeypatch.delenv(var)


def test_in_window(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    s = x_settings()
    assert in_window(s, et(2026, 10, 12, 9, 0))            # Monday 09:00 ET: inclusive start
    assert not in_window(s, et(2026, 10, 12, 8, 59))
    assert in_window(s, et(2026, 10, 16, 16, 29))          # Friday
    assert not in_window(s, et(2026, 10, 16, 16, 30))      # exclusive end
    assert not in_window(s, et(2026, 10, 17, 12, 0))       # Saturday


def test_query_and_body():
    assert build_query(("A", "b_c")) == "from:A OR from:b_c -filter:replies"
    body = build_body(("A",))
    assert body["max_tool_calls"] == 1 and body["tools"] == [{"type": "x_search", "allowed_x_handles": ["A"]}]
    assert body["model"] == x_feed.XAI_MODEL and "x_keyword_search" in body["input"][0]["content"]


def test_extract_text_and_cost():
    p = payload([])
    assert json.loads(extract_text(p)) == {"posts": []}
    assert extract_text({"output_text": "{}"}) == "{}"
    assert call_cost_usd(p) == pytest.approx(0.055) and call_cost_usd({}) == x_feed.ASSUMED_CALL_COST_USD


def test_parse_posts_rejects_foreign_out_of_window_and_junk():
    now = 1_700_000_000.0
    ok = snowflake_at(now - 30); old = snowflake_at(now - 3600); future = snowflake_at(now + 600)
    posts = [{"url": f"https://x.com/Reuters/status/{ok}", "text": "BREAKING: thing happened"},
             {"url": f"https://twitter.com/reuters/status/{ok}", "text": "same post, other domain"},   # duplicate id
             {"url": f"https://x.com/elonmusk/status/{snowflake_at(now - 10)}", "text": "foreign"},
             {"url": f"https://x.com/Reuters/status/{old}", "text": "stale"},
             {"url": f"https://x.com/Reuters/status/{future}", "text": "hallucinated id"},
             {"url": "https://x.com/Reuters", "text": "no status id"},
             {"url": f"https://x.com/Reuters/status/{snowflake_at(now - 20)}", "text": "   "},
             "not a dict"]
    out, counts = parse_posts(json.dumps({"posts": posts}), ("Reuters",), now - 120, now + 60)
    assert [p["id"] for p in out] == [ok] and out[0]["url"] == f"https://x.com/Reuters/status/{ok}"
    assert out[0]["handle"] == "Reuters" and out[0]["ts"] == pytest.approx(now - 30, abs=1e-3)
    assert counts == {"accepted": 1, "foreign": 1, "out_of_window": 2, "malformed": 2, "duplicate": 1, "empty": 1,
                      "parse_error": False}


def test_parse_posts_fenced_and_broken():
    now = 1_700_000_000.0
    sid = snowflake_at(now - 5)
    fenced = "```json\n" + json.dumps({"posts": [{"url": f"https://x.com/A/status/{sid}", "text": "x"}]}) + "\n```"
    assert len(parse_posts(fenced, ("A",), now - 60, now + 60)[0]) == 1
    out, counts = parse_posts("I could not find anything.", ("A",), now - 60, now + 60)
    assert out == [] and counts["parse_error"] is True
    assert parse_posts(json.dumps({"posts": [{"url": f"https://x.com/A/status/{sid}", "text": "x"}]}),
                       ("A",), now - 60, now + 60, seen={sid})[1]["duplicate"] == 1


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def post(self, url, json=None, **kw):
        self.calls.append((url, json))
        status, body = self.responses.pop(0)

        class R:
            status_code = status
            text = ""

            def json(self_inner):
                return body
        return R()

    async def aclose(self):
        pass


def make_feed(ledger, monkeypatch, responses, clock, handles="Reuters", budget="25"):
    monkeypatch.setenv("XAI_API_KEY", "k"); monkeypatch.setenv("XAI_X_HANDLES", handles)
    monkeypatch.setenv("XAI_DAILY_BUDGET_USD", budget)
    q, stats = asyncio.Queue(), {}
    return XFeed(q, stats, ledger, client=FakeClient(responses), now=lambda: clock[0]), q, stats


def test_first_poll_is_backlog_then_emits(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 10, 0)]
    a, b = snowflake_at(clock[0] - 20), snowflake_at(clock[0] + 40)
    feed, q, stats = make_feed(tmp_ledger, monkeypatch, [
        (200, payload([{"url": f"https://x.com/Reuters/status/{a}", "text": "first"}])),
        (200, payload([{"url": f"https://x.com/Reuters/status/{a}", "text": "first"},
                       {"url": f"https://x.com/Reuters/status/{b}", "text": "second <b>bold</b>"}]))], clock)
    assert feed.gate() is None
    assert asyncio.run(feed.poll_once()) == 0 and q.empty()            # backlog: seen, not traded
    clock[0] += 60
    assert asyncio.run(feed.poll_once()) == 1
    ev = q.get_nowait()
    assert ev["id"] == f"x-{b}" and ev["source"] == "x:Reuters" and ev["headline"] == "second bold"
    assert ev["published_ts"] == pytest.approx(decode_snowflake(b)) and ev["seen_ts"] == clock[0]
    assert ev["url"] == f"https://x.com/Reuters/status/{b}" and ev["summary"] == ""
    assert stats["x"]["polls"] == 2 and stats["x"]["new"] == 1
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (2, pytest.approx(0.11))


def test_gap_after_window_is_backlog_again(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 16, 0)]
    feed, q, _ = make_feed(tmp_ledger, monkeypatch, [(200, payload([])), (200, payload([]))], clock)
    asyncio.run(feed.poll_once())
    clock[0] = et(2026, 10, 13, 9, 0)                                   # next morning: first call after a long gap
    a2 = snowflake_at(clock[0] - 10)
    feed.client.responses[0] = (200, payload([{"url": f"https://x.com/Reuters/status/{a2}", "text": "morning"}]))
    assert asyncio.run(feed.poll_once()) == 0 and q.empty()


def test_gate_outside_window_and_budget_persists(tmp_path, monkeypatch, capsys):
    L = Ledger(tmp_path / "l.db")
    clock = [et(2026, 10, 17, 12, 0)]                                    # Saturday
    feed, _, stats = make_feed(L, monkeypatch, [], clock, budget="0.10")
    assert feed.gate() == "outside_window" and stats["x"]["in_window"] is False
    clock[0] = et(2026, 10, 12, 12, 0)
    assert feed.gate() is None
    L.x_spend_add(utc_day(clock[0]), 0.05)                               # 0.05 + 0.06 estimate > 0.10
    assert feed.gate() == "budget_hit" and feed.gate() == "budget_hit"
    assert capsys.readouterr().out.count("daily budget") == 1            # warned once
    assert stats["x"]["budget_hit"] is True and L.feed_status("x")["info"]["budget_hit"] is True
    feed2, _, _ = make_feed(Ledger(tmp_path / "l.db"), monkeypatch, [], clock, budget="0.10")
    assert feed2.gate() == "budget_hit"                                  # survives a restart (ledger, not memory)
    clock[0] = et(2026, 10, 13, 12, 0)
    assert feed.gate() is None                                           # new UTC day


def test_http_error_counts_and_raises(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    feed, _, stats = make_feed(tmp_ledger, monkeypatch, [(429, {})], clock)
    with pytest.raises(RuntimeError):
        asyncio.run(feed.poll_once())
    assert stats["x"]["errors"] == 1 and tmp_ledger.x_spend(utc_day(clock[0])) == (0, 0.0)


def test_disabled_feed_run_returns(tmp_ledger):
    feed = XFeed(asyncio.Queue(), {}, tmp_ledger)
    assert feed.enabled is False
    assert asyncio.run(asyncio.wait_for(feed.run(), 2)) is None


def test_http_error_with_usage_is_charged(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    feed, _, stats = make_feed(tmp_ledger, monkeypatch, [(500, {"usage": {"cost_in_usd_ticks": 100_000_000}})], clock)
    with pytest.raises(RuntimeError):
        asyncio.run(feed.poll_once())
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (1, pytest.approx(0.01))


def test_parse_error_is_still_paid_and_emits_nothing(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    bad = {"output_text": "sorry, no JSON here", "usage": {"cost_in_usd_ticks": 550_000_000}}
    feed, q, stats = make_feed(tmp_ledger, monkeypatch, [(200, bad)], clock)
    assert asyncio.run(feed.poll_once()) == 0 and q.empty()
    assert stats["x"]["rejected"]["parse_error"] is True
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (1, pytest.approx(0.055))


def test_url_rebuilt_from_handle_and_id_not_model_string(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 10, 0)]
    a, b = snowflake_at(clock[0] - 20), snowflake_at(clock[0] + 30)
    first = payload([{"url": f"https://x.com/Reuters/status/{a}", "text": "seed"}])
    evil = payload([{"url": f"https://www.twitter.com/REUTERS/status/{b}?utm=evil&x=http://bad.test", "text": "news"}])
    feed, q, _ = make_feed(tmp_ledger, monkeypatch, [(200, first), (200, evil)], clock)
    asyncio.run(feed.poll_once())
    clock[0] += 60
    assert asyncio.run(feed.poll_once()) == 1
    ev = q.get_nowait()
    assert ev["url"] == f"https://x.com/Reuters/status/{b}" and ev["source"] == "x:Reuters"


def test_foreign_and_hallucinated_rejected_end_to_end(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 10, 0)]
    seed = snowflake_at(clock[0] - 5)
    ok = snowflake_at(clock[0] + 20)
    hallucinated = snowflake_at(clock[0] - 86400 * 400)          # training-data post from long ago
    feed, q, stats = make_feed(tmp_ledger, monkeypatch, [
        (200, payload([{"url": f"https://x.com/Reuters/status/{seed}", "text": "seed"}])),
        (200, payload([{"url": f"https://x.com/someoneelse/status/{ok}", "text": "foreign"},
                       {"url": f"https://x.com/Reuters/status/{hallucinated}", "text": "old"},
                       {"url": f"https://x.com/Reuters/status/{ok}", "text": "real"}]))], clock)
    asyncio.run(feed.poll_once())
    clock[0] += 60
    assert asyncio.run(feed.poll_once()) == 1
    assert q.get_nowait()["headline"] == "real" and q.empty()
    assert stats["x"]["rejected"]["foreign"] == 1 and stats["x"]["rejected"]["out_of_window"] == 1


def test_duplicate_post_across_polls_emitted_once(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 10, 0)]
    a, b = snowflake_at(clock[0] - 5), snowflake_at(clock[0] + 30)
    p_b = {"url": f"https://x.com/Reuters/status/{b}", "text": "second"}
    feed, q, _ = make_feed(tmp_ledger, monkeypatch, [
        (200, payload([{"url": f"https://x.com/Reuters/status/{a}", "text": "seed"}])),
        (200, payload([p_b])), (200, payload([p_b]))], clock)
    asyncio.run(feed.poll_once())
    clock[0] += 60
    assert asyncio.run(feed.poll_once()) == 1
    clock[0] += 60
    assert asyncio.run(feed.poll_once()) == 0 and q.qsize() == 1


def test_headline_capped_at_280(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 10, 0)]
    a, b = snowflake_at(clock[0] - 5), snowflake_at(clock[0] + 30)
    feed, q, _ = make_feed(tmp_ledger, monkeypatch, [
        (200, payload([{"url": f"https://x.com/Reuters/status/{a}", "text": "seed"}])),
        (200, payload([{"url": f"https://x.com/Reuters/status/{b}", "text": "w" * 500}]))], clock)
    asyncio.run(feed.poll_once())
    clock[0] += 60
    asyncio.run(feed.poll_once())
    assert len(q.get_nowait()["headline"]) == 280


def test_budget_hit_after_spend_exceeds_via_polls(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    feed, _, _ = make_feed(tmp_ledger, monkeypatch, [(200, payload([])), (200, payload([]))], clock, budget="0.15")
    assert feed.gate() is None
    asyncio.run(feed.poll_once())
    clock[0] += 60
    assert feed.gate() is None                # 0.055 + 0.06 estimate <= 0.15
    asyncio.run(feed.poll_once())
    clock[0] += 60
    assert feed.gate() == "budget_hit"        # 0.11 + 0.06 > 0.15: stops one call early, never over


def _raising_feed(ledger, monkeypatch, exc, clock):
    feed, _, stats = make_feed(ledger, monkeypatch, [], clock)

    async def boom(url, json=None, **kw):
        raise exc
    feed.client.post = boom
    return feed, stats


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t"),
                                 httpx.ReadError("r"), httpx.RemoteProtocolError("p")])
def test_sent_but_failed_call_is_charged_and_reraised(tmp_ledger, monkeypatch, exc):
    clock = [et(2026, 10, 12, 10, 0)]
    feed, stats = _raising_feed(tmp_ledger, monkeypatch, exc, clock)
    with pytest.raises(type(exc)):
        asyncio.run(feed.poll_once())
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (1, pytest.approx(0.06)) and stats["x"]["polls"] == 1


@pytest.mark.parametrize("exc", [httpx.ConnectError("c"), httpx.ConnectTimeout("t")])
def test_connect_failure_is_not_charged(tmp_ledger, monkeypatch, exc):
    clock = [et(2026, 10, 12, 10, 0)]
    feed, stats = _raising_feed(tmp_ledger, monkeypatch, exc, clock)
    with pytest.raises(type(exc)):
        asyncio.run(feed.poll_once())
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (0, 0.0)


def test_pre_call_estimate_uses_most_expensive_call_today(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    feed, _, _ = make_feed(tmp_ledger, monkeypatch, [(200, payload([], ticks=1_650_000_000))], clock, budget="0.30")
    assert feed.gate() is None
    asyncio.run(feed.poll_once())              # costs 0.165
    clock[0] += 60
    assert feed.gate() == "budget_hit"         # 0.165 + max(0.06, 0.165) > 0.30 (a fixed 0.06 would allow it)
    clock[0] += 86400                          # next UTC day: record resets
    assert feed._estimate(utc_day(clock[0])) == 0.06


def test_settings_repr_hides_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-SECRET-distinct-123")
    assert "SECRET" not in repr(x_settings())


def test_post_url_id_is_end_anchored():
    now = time.time()
    sid = snowflake_at(now)
    url = f"https://x.com/A/status/{'1' * 23}"
    out, counts = parse_posts(json.dumps({"posts": [{"url": url, "text": "x"}]}), ("A",), 0, 1e12)
    assert out == [] and counts["malformed"] == 1
    out, _ = parse_posts(json.dumps({"posts": [{"url": f"https://x.com/A/status/{sid}", "text": "x"}]}),
                         ("A",), now - 60, now + 60)
    assert len(out) == 1


def test_in_window_across_dst(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    s = x_settings()
    utc = lambda y, mo, d, h, mi: datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC")).timestamp()
    assert in_window(s, utc(2026, 3, 9, 13, 0)) and not in_window(s, utc(2026, 3, 9, 12, 59))     # EDT from Mar 8
    assert in_window(s, utc(2026, 11, 2, 14, 0)) and not in_window(s, utc(2026, 11, 2, 13, 59))   # EST from Nov 1
    assert not in_window(s, utc(2026, 11, 2, 13, 30))


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t"),
                                 httpx.ReadError("r"), httpx.RemoteProtocolError("p")])
def test_timeouts_and_drops_increment_errors_counter(tmp_ledger, monkeypatch, exc):
    clock = [et(2026, 10, 12, 10, 0)]
    feed, stats = _raising_feed(tmp_ledger, monkeypatch, exc, clock)
    for n in (1, 2):
        with pytest.raises(type(exc)):
            asyncio.run(feed.poll_once())
        assert stats["x"]["errors"] == n


@pytest.mark.parametrize("exc", [httpx.ConnectError("c"), httpx.ConnectTimeout("t")])
def test_connect_failure_does_not_touch_charge_or_spend(tmp_ledger, monkeypatch, exc):
    clock = [et(2026, 10, 12, 10, 0)]
    feed, stats = _raising_feed(tmp_ledger, monkeypatch, exc, clock)
    with pytest.raises(type(exc)):
        asyncio.run(feed.poll_once())
    assert tmp_ledger.x_spend(utc_day(clock[0])) == (0, 0.0)


def test_gate_heartbeat_carries_budget_usd(tmp_ledger, monkeypatch):
    clock = [et(2026, 10, 12, 12, 0)]
    feed, _, _ = make_feed(tmp_ledger, monkeypatch, [], clock, budget="7.5")
    assert feed.gate() is None
    info = tmp_ledger.feed_status("x")["info"]
    assert info["enabled"] is True and info["budget_usd"] == 7.5
