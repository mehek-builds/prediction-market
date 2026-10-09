"""Resting paper orders (ENTRY_STYLE=post): limit, size and fill model, the PaperOrders lifecycle, and the engine wiring.
Offline: fake book fetcher, injectable clock, MockTransport for the real-money recorder."""
import asyncio
import json

import pytest

from fastlane import engine as engine_mod
from fastlane import live
from fastlane.books import (MAX_ENTRY_PRICE, MIN_ENTRY_PRICE, TICK, Book, kalshi_taker_fee, post_fill, post_limit,
                            post_size)
from fastlane.orders import PaperOrders

from test_engine_handle import LEAN, FakeJev, _answers_for, _event, _make_engine
from test_live import FILLED, Recorder
from test_live import _engine as _live_engine


def kb(yes_asks=(), no_asks=(), venue="kalshi"):
    return Book(venue, "M", list(yes_asks), list(no_asks))


# ---------- post_limit ----------
def test_post_limit_is_bid_plus_one_tick():
    assert TICK == 0.01
    assert post_limit(kb([(.55, 10)], [(.47, 10)]), "yes") == pytest.approx(.54)     # bid .53


def test_post_limit_joins_the_bid_on_a_one_tick_spread():
    assert post_limit(kb([(.55, 10)], [(.46, 10)]), "yes") == pytest.approx(.54)     # bid .54, ask .55: never cross


def test_post_limit_no_side_uses_the_no_bid():
    b = kb([(.47, 10)], [(.55, 10)])   # NO bid = 1 - .47 = .53, NO ask .55
    assert post_limit(b, "no") == pytest.approx(.54)


def test_post_limit_none_without_bid():
    assert post_limit(kb([(.55, 10)], []), "yes") is None
    assert post_limit(kb([], [(.47, 10)]), "no") is None


def test_post_limit_without_ask_still_bids_one_tick_up():
    assert post_limit(kb([], [(.47, 10)]), "yes") == pytest.approx(.54)


def test_post_limit_none_at_or_above_max_entry():
    assert MAX_ENTRY_PRICE == .95
    assert post_limit(kb([(.97, 10)], [(.05, 10)]), "yes") is None      # bid .95 -> limit .96
    # bid .94 -> limit .95 == MAX_ENTRY_PRICE -> refused
    assert post_limit(kb([(.99, 10)], [(.06, 10)]), "yes") is None
    assert post_limit(kb([(.99, 10)], [(.07, 10)]), "yes") == pytest.approx(.94)


def test_post_limit_none_below_min_entry():
    assert MIN_ENTRY_PRICE == .03
    assert post_limit(kb([(.05, 10)], [(.98, 10)]), "yes") == pytest.approx(.03)   # bid .02 -> limit .03: allowed
    assert post_limit(kb([(.05, 10)], [(.99, 10)]), "yes") is None       # bid .01 -> limit .02 < .03


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_post_limit_both_venues(venue):
    assert post_limit(kb([(.55, 10)], [(.47, 10)], venue), "yes") == pytest.approx(.54)


# ---------- post_size ----------
def test_post_size_kalshi_floors_to_whole_contracts():
    assert post_size("kalshi", .54, 54.0) == 100.0
    assert post_size("kalshi", .54, 20.0) == 37.0
    assert post_size("kalshi", .54, .53) == 0.0         # under one contract


def test_post_size_polymarket_two_decimals_never_above_budget():
    n = post_size("polymarket", .54, 20.0)
    assert n == pytest.approx(37.03) and n * .54 <= 20.0
    assert post_size("polymarket", .54, .30) == pytest.approx(.55)


# ---------- post_fill ----------
def test_post_fill_none_when_ask_above_limit():
    assert post_fill("yes", .54, 100, kb([(.55, 500)]), "kalshi") is None
    assert post_fill("yes", .54, 100, kb([]), "kalshi") is None


def test_post_fill_partial_at_displayed_size():
    f = post_fill("yes", .54, 100, kb([(.54, 30)]), "kalshi")
    assert f == {"contracts": 30.0, "price": .54, "ask_seen": .54, "qty_seen": 30}


def test_post_fill_never_better_than_the_limit():
    f = post_fill("yes", .54, 100, kb([(.52, 500)]), "kalshi")
    assert f["contracts"] == 100.0 and f["price"] == .54 and f["ask_seen"] == .52


def test_post_fill_sums_levels_at_or_below_limit_only():
    f = post_fill("yes", .54, 100, kb([(.52, 10), (.54, 20), (.56, 1000)]), "kalshi")
    assert f["contracts"] == 30.0 and f["qty_seen"] == 30 and f["ask_seen"] == .52


def test_post_fill_kalshi_whole_contracts_polymarket_fractional():
    assert post_fill("yes", .54, 100, kb([(.54, 30.7)]), "kalshi")["contracts"] == 30.0
    assert post_fill("yes", .54, 100, kb([(.54, 30.7)], venue="polymarket"), "polymarket")["contracts"] == pytest.approx(30.7)
    assert post_fill("yes", .54, 100, kb([(.54, .4)]), "kalshi") is None     # less than one whole contract


def test_post_fill_no_side_reads_no_asks():
    assert post_fill("no", .54, 10, kb([(.40, 500)], [(.60, 500)]), "kalshi") is None
    assert post_fill("no", .54, 10, kb([(.60, 500)], [(.54, 500)]), "kalshi")["contracts"] == 10.0


# ---------- PaperOrders lifecycle ----------
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Feed:
    """Fake fetch_book: returns whatever `book` currently is and counts calls."""
    def __init__(self, book):
        self.book, self.calls = book, 0

    async def __call__(self, http, market):
        self.calls += 1
        return self.book


MARKET = {"venue": "kalshi", "id": "MK-1", "question": "Q?"}
EV = {"id": "ev1"}
BOOK0 = kb([(.55, 1000)], [(.47, 1000)])      # yes bid .53 -> limit .54


def _orders(ledger, feed, clock, monkeypatch=None, **kw):
    fills = []
    po = PaperOrders(ledger, None, fetch_book=feed, on_fill=lambda o, tid, m: fills.append((o, tid, m)), now=clock, **kw)
    return po, fills


def _place(po, book_name="shadow", size_usd=54.0, market=MARKET, bk=BOOK0, side="yes"):
    return po.place(book=book_name, ev=EV, market=market, side=side, bk=bk, size_usd=size_usd,
                    strength=.9, decisive=.2, synthetic=False)


def test_place_writes_a_working_row(tmp_ledger):
    clock = Clock()
    po, _ = _orders(tmp_ledger, Feed(BOOK0), clock)
    row = _place(po)
    assert row["limit_price"] == pytest.approx(.54) and row["requested"] == 100.0 and row["take_price"] == .55
    w = tmp_ledger.orders_working()
    assert len(w) == 1 and w[0]["status"] == "working" and w[0]["style"] == "post" and w[0]["book"] == "shadow"
    assert w[0]["expires_ts"] == pytest.approx(1000.0 + po.max_wait_s)
    assert "MK-1" in po.watch and tmp_ledger.open_order_markets("shadow") == {"MK-1"}
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_place_returns_none_and_no_row_when_no_limit_or_no_size(tmp_ledger):
    po, _ = _orders(tmp_ledger, Feed(BOOK0), Clock())
    assert _place(po, bk=kb([(.55, 10)], [])) is None                # no bid
    assert _place(po, size_usd=.30) is None                            # not one whole contract
    assert tmp_ledger.orders_working() == [] and po.watch == set()


def test_tick_above_limit_does_nothing_and_tick_at_limit_snapshots_but_never_fills_alone(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.55, 1000)], [(.47, 1000)]))     # the snapshot still says ask .55
    po, fills = _orders(tmp_ledger, feed, clock)
    _place(po)

    async def go():
        po.on_tick("MK-1", clock.t, .53, .55)             # ask above limit
        po.on_tick("OTHER", clock.t, .53, .54)            # not our ticker
        assert not po._tasks and feed.calls == 0
        po.on_tick("MK-1", clock.t, .53, .54)             # ask at limit: snapshot scheduled
        assert len(po._tasks) == 1
        await asyncio.gather(*list(po._tasks))
        assert feed.calls == 1
    asyncio.run(go())
    assert fills == [] and tmp_ledger.orders_working()[0]["filled"] == 0       # snapshot moved back: no fill


def test_tick_no_side_uses_one_minus_yes_bid(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.47, 10)], [(.60, 10)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    assert _place(po, side="no", bk=kb([(.47, 10)], [(.55, 10)]))["limit_price"] == pytest.approx(.54)

    async def go():
        po.on_tick("MK-1", clock.t, .40, .45)              # NO ask = .60 > .54
        assert not po._tasks
        po.on_tick("MK-1", clock.t, .46, .50)              # NO ask = .54
        assert len(po._tasks) == 1
        await asyncio.gather(*list(po._tasks))
    asyncio.run(go())


def test_tick_wakes_snapshot_and_fill_creates_trade_mark_and_callback_once(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 1000)], [(.46, 1000)]))
    po, fills = _orders(tmp_ledger, feed, clock)
    _place(po)

    async def go():
        po.on_tick("MK-1", clock.t + 1, .53, .54)
        await asyncio.gather(*list(po._tasks))
    clock.t += 1
    asyncio.run(go())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "filled" and o["filled"] == 100.0 and o["avg_price"] == .54 and o["trade_id"]
    t = tmp_ledger.db.execute("SELECT book, entry_style, order_id, limit_price, contracts, avg_price, shadow, best_ask "
                              "FROM trades").fetchall()
    assert t == [("shadow", "post", 1, .54, 100.0, .54, 1, .55)]
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM marks WHERE event_id = 'shadow:ev1' AND horizon_s = 0").fetchone()[0] == 1
    assert len(fills) == 1 and fills[0][1] == o["trade_id"]
    assert po.watch == set() and po.working() == []


def test_snapshot_at_limit_fills_via_poll(tmp_ledger):
    feed = Feed(kb([(.54, 1000)], [(.46, 1000)]))
    po, fills = _orders(tmp_ledger, feed, Clock())
    _place(po, book_name="starter")
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["status"] == "filled"
    assert tmp_ledger.db.execute("SELECT book, shadow FROM trades").fetchone() == ("starter", 0)
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM marks WHERE event_id = 'starter:ev1'").fetchone()[0] == 1
    assert len(fills) == 1


def test_no_fill_while_ask_stays_above_limit(tmp_ledger):
    po, fills = _orders(tmp_ledger, Feed(BOOK0), Clock())
    _place(po)
    asyncio.run(po.poll_once())
    assert tmp_ledger.orders_working()[0]["filled"] == 0 and fills == []
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_partial_then_full_updates_one_trade_row_and_two_fill_rows(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 30)], [(.46, 30)]))
    po, fills = _orders(tmp_ledger, feed, clock)
    _place(po)
    asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "working" and o["filled"] == 30.0
    feed.book = kb([(.53, 500)], [(.46, 500)])
    clock.t += 5
    asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "filled" and o["filled"] == 100.0 and o["closed_ts"] == clock.t
    trades = tmp_ledger.db.execute("SELECT contracts, avg_price, cost, fee FROM trades").fetchall()
    assert len(trades) == 1 and trades[0][0] == 100.0 and trades[0][1] == .54 and trades[0][2] == pytest.approx(54.0)
    assert trades[0][3] == pytest.approx(kalshi_taker_fee(30, .54) + kalshi_taker_fee(70, .54))   # Kalshi rounds per fill
    f = tmp_ledger.db.execute("SELECT contracts, price FROM paper_fills ORDER BY id").fetchall()
    assert f == [(30.0, .54), (70.0, .54)]
    assert len(fills) == 1             # on_fill only on the first fill


def test_partial_then_expiry_keeps_the_partial_trade(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 30)], [(.46, 30)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    _place(po)
    asyncio.run(po.poll_once())
    feed.book = BOOK0
    clock.t += po.max_wait_s + 1
    asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "partial_expired" and o["filled"] == 30.0
    assert tmp_ledger.db.execute("SELECT contracts FROM trades").fetchall() == [(30.0,)]
    assert po.working() == [] and po.watch == set()


def test_no_fill_then_expiry_is_post_expired_without_a_trade(tmp_ledger):
    clock = Clock()
    po, fills = _orders(tmp_ledger, Feed(BOOK0), clock)
    _place(po)
    clock.t += po.max_wait_s - 1
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["status"] == "working"
    clock.t += 2
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["status"] == "post_expired" and tmp_ledger.order_get(1)["closed_ts"] == clock.t
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0 and fills == []
    assert tmp_ledger.orders_recent(10, 0)[0]["status"] == "post_expired"


def test_post_max_wait_env_is_honoured(tmp_ledger, monkeypatch):
    monkeypatch.setenv("POST_MAX_WAIT_S", "60")
    clock = Clock()
    po, _ = _orders(tmp_ledger, Feed(BOOK0), clock)
    assert _place(po)["expires_ts"] == pytest.approx(1060.0)


def test_fetch_error_on_one_market_does_not_stop_the_others(tmp_ledger):
    clock = Clock()
    good = kb([(.54, 1000)], [(.46, 1000)])
    other = {"venue": "kalshi", "id": "MK-2", "question": "Q2"}

    async def fetch(http, market):
        if market["id"] == "MK-1":
            raise RuntimeError("boom")
        return good
    po, _ = _orders(tmp_ledger, fetch, clock)
    _place(po)
    _place(po, market=other)
    asyncio.run(po.poll_once())
    st = {r["market_id"]: r["status"] for r in tmp_ledger._orders("")}
    assert st == {"MK-1": "working", "MK-2": "filled"}


def test_withdraw_all_and_restart_cleanup(tmp_ledger):
    po, _ = _orders(tmp_ledger, Feed(BOOK0), Clock())
    _place(po)
    _place(po, market={"venue": "kalshi", "id": "MK-2", "question": "Q"})
    assert po.withdraw_all("engine_stop") == 2
    assert {r["status"] for r in tmp_ledger._orders("")} == {"withdrawn"} and po.watch == set()
    # a leftover working row from a previous session is withdrawn by the next process, never resumed
    tmp_ledger.order_place(book="live", market_id="MK-9", venue="kalshi", status="working", side="yes", limit_price=.5,
                           requested=10, filled=0, created_ts=1.0, expires_ts=9e9, synthetic=0)
    po2, _ = _orders(tmp_ledger, Feed(BOOK0), Clock())
    assert po2.withdraw_all("restart") == 1
    row = [r for r in tmp_ledger._orders("") if r["market_id"] == "MK-9"][0]
    assert row["status"] == "withdrawn" and row["note"] == "restart"


def test_post_max_working_marks_the_queue_full(tmp_ledger, monkeypatch):
    monkeypatch.setenv("POST_MAX_WORKING", "2")
    po, _ = _orders(tmp_ledger, Feed(BOOK0), Clock())
    assert po.max_working == 2 and not po.full()
    _place(po)
    _place(po, market={"venue": "kalshi", "id": "MK-2", "question": "Q"})
    assert po.full()


def test_fee_kalshi_matches_taker_formula_polymarket_zero(tmp_ledger):
    po, _ = _orders(tmp_ledger, Feed(kb([(.54, 1000)], [(.46, 1000)])), Clock())
    _place(po)
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["fee"] == kalshi_taker_fee(100, .54) > 0
    pm = {"venue": "polymarket", "id": "PM-1", "question": "Q", "yes_token": "t"}
    pbook = kb([(.54, 1000)], [(.46, 1000)], "polymarket")
    po2, _ = _orders(tmp_ledger, Feed(pbook), Clock())
    po2.place(book="shadow", ev={"id": "ev2"}, market=pm, side="yes", bk=kb([(.55, 10)], [(.47, 10)], "polymarket"),
              size_usd=20.0, strength=.9, decisive=.1, synthetic=False)
    asyncio.run(po2.poll_once())
    row = tmp_ledger.order_get(2)
    assert row["fee"] == 0.0 and row["requested"] == pytest.approx(37.03) and row["status"] == "filled"


# ---------- engine wiring ----------
def _run_flow(e, ev, then=None):
    """handle -> drain side books -> optional coroutine `then` (same loop) -> cleanup."""
    async def go():
        try:
            out = await e.handle(ev)
            await e.drain_shadow()
            await e.drain_starter()
            if then:
                await then()
            return out
        finally:
            for t in list(e._bg):
                t.cancel()
            await asyncio.gather(*list(e._bg), return_exceptions=True)
            await e.http.aclose()
    return asyncio.run(go())


def test_engine_live_post_rests_then_fills_on_snapshot(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    assert e.styles["live"] == "post"

    async def then():
        assert e.ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        assert len(e.orders.working()) == 1
        # the market comes to us: ask .54 displayed
        engine_mod_fetch[0] = Book("kalshi", "AVNT-1", [(.54, 1000)], [(.46, 1000)])
        await e.orders.poll_once()

    engine_mod_fetch = [None]
    base = engine_mod.fetch_book

    async def fake(client, market):
        return engine_mod_fetch[0] or Book("kalshi", market["id"], [(.55, 1000)], [(.47, 1000)])
    monkeypatch.setattr(engine_mod, "fetch_book", fake)
    rec = _run_flow(e, _event(), then)
    assert (rec["action"], rec["reason"]) == ("BUY_YES", "post_working")
    row = e.ledger.db.execute("SELECT book, entry_style, limit_price, order_id FROM trades").fetchall()
    assert row == [("live", "post", .54, 1)]
    assert e.trades == 1 and base is not None
    assert e.ledger.db.execute("SELECT action, reason FROM decisions WHERE event_id='ev1'").fetchone() == ("BUY_YES", "post_working")


def test_engine_queue_full_blocks_with_post_queue_full(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    e.orders.max_working = 0
    rec = _run_flow(e, _event())
    assert rec["reason"] == "post_queue_full" and e.ledger.orders_working() == []


def test_working_order_on_the_market_blocks_a_second_entry(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    _run_flow(e, _event())
    assert e.ledger.traded_markets("live") == {"AVNT-1"}
    import httpx
    e.http = httpx.AsyncClient()
    rec2 = _run_flow(e, _event(id="ev2"))
    assert rec2["reason"] == "already_in_market" and len(e.ledger.orders_working()) == 1


def test_synthetic_event_fills_take_even_with_post_everywhere(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)
    rec = _run_flow(e, _event(synthetic=True))
    assert rec["reason"] == "signal_yes"
    assert e.ledger.db.execute("SELECT entry_style, book FROM trades").fetchall() == [("take", "live")]
    assert e.ledger.orders_working() == []


def test_armed_real_money_forces_take_on_kalshi_and_sends_one_order(monkeypatch, tmp_path, tiny_universe, rsa_pem, capsys):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    rec_ = Recorder(FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    live.arm(e.live.session)
    out = _run_flow(e, _event())
    assert out["reason"] == "signal_yes"                       # a fill, not post_working
    assert e.ledger.orders_working() == []
    assert e.ledger.db.execute("SELECT entry_style FROM trades WHERE book = 'live'").fetchall() == [("take",)]
    assert len(rec_.requests) == 1
    assert json.loads(rec_.requests[0].content)["price"] == "0.5800"     # the paper take limit (ask .55 + 3c)


def test_armed_real_money_post_fill_in_shadow_sends_nothing(monkeypatch, tmp_path, tiny_universe, rsa_pem):
    rec_ = Recorder(FILLED, FILLED)
    e = _live_engine(monkeypatch, tmp_path, tiny_universe, rsa_pem, rec_)
    e.styles["shadow"] = "post"
    e.jev = FakeJev(_answers_for(probs=LEAN))
    live.arm(e.live.session)
    assert e.live.is_live()

    async def then():
        assert {r["book"] for r in e.orders.working()} == {"shadow", "starter"}    # real PASS: both side books rest
        engine_book[0] = Book("kalshi", "AVNT-1", [(.54, 1000)], [(.46, 1000)])
        await e.orders.poll_once()

    engine_book = [None]

    async def fake(client, market):
        return engine_book[0] or Book("kalshi", market["id"], [(.55, 1000)], [(.47, 1000)])
    monkeypatch.setattr(engine_mod, "fetch_book", fake)
    _run_flow(e, _event(), then)
    assert sorted(e.ledger.db.execute("SELECT book, entry_style FROM trades").fetchall()) == \
        [("shadow", "post"), ("starter", "post")]
    assert rec_.requests == [] and e.ledger.db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_stop_withdraws_working_orders(monkeypatch, tmp_path, tiny_universe):
    monkeypatch.setenv("ENTRY_STYLE_LIVE", "post")
    e = _make_engine(monkeypatch, tmp_path, tiny_universe)

    async def go():
        await e.handle(_event())
        assert len(e.orders.working()) == 1
        e.live.stop = lambda: None
        await e.stop()
    asyncio.run(go())
    assert e.ledger.db.execute("SELECT status, note FROM paper_orders").fetchall() == [("withdrawn", "engine_stop")]


def test_persistent_displayed_size_is_credited_once(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 10)], [(.46, 10)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    _place(po)                                                   # 100 contracts at .54
    for _ in range(5):
        clock.t += 2
        asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "working" and o["filled"] == 10.0     # the same 10 resting contracts, five times
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1
    clock.t += po.max_wait_s
    asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "partial_expired" and o["filled"] == 10.0


def test_only_an_increase_in_displayed_size_is_credited(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 10)], [(.46, 10)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    _place(po)
    asyncio.run(po.poll_once())
    feed.book = kb([(.54, 25)], [(.46, 25)])
    clock.t += 2
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["filled"] == 25.0
    feed.book = kb([(.54, 5)], [(.46, 5)])                       # shrank: nothing new
    clock.t += 2
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["filled"] == 25.0


def test_tick_woken_check_and_poll_share_the_credited_state(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 10)], [(.46, 10)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    _place(po)
    asyncio.run(po._check(1))
    asyncio.run(po.poll_once())
    asyncio.run(po._check(1))
    assert tmp_ledger.order_get(1)["filled"] == 10.0


def test_snapshot_taken_after_expiry_never_fills(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 1000)], [(.46, 1000)]))
    po, fills = _orders(tmp_ledger, feed, clock)
    _place(po)
    clock.t += 10_000
    asyncio.run(po.poll_once())
    o = tmp_ledger.order_get(1)
    assert o["status"] == "post_expired" and o["filled"] == 0 and fills == []
    assert tmp_ledger.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_tick_woken_check_after_expiry_never_fills(tmp_ledger):
    clock, feed = Clock(), Feed(kb([(.54, 1000)], [(.46, 1000)]))
    po, _ = _orders(tmp_ledger, feed, clock)
    _place(po)
    clock.t += 10_000
    asyncio.run(po._check(1))
    assert tmp_ledger.order_get(1)["status"] == "post_expired" and tmp_ledger.order_get(1)["filled"] == 0


def test_a_snapshot_requested_before_expiry_but_landing_after_does_not_fill(tmp_ledger):
    clock = Clock()

    async def slow(http, market):
        clock.t += po.max_wait_s + 5                              # the fetch itself runs past expiry
        return kb([(.54, 1000)], [(.46, 1000)])
    po, _ = _orders(tmp_ledger, slow, clock)
    _place(po)
    asyncio.run(po.poll_once())
    assert tmp_ledger.order_get(1)["status"] == "post_expired" and tmp_ledger.order_get(1)["filled"] == 0


def test_books_are_fetched_concurrently(tmp_ledger):
    clock = Clock()
    active, peak = 0, 0

    async def fetch(http, market):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return BOOK0
    po, _ = _orders(tmp_ledger, fetch, clock)
    _place(po)
    _place(po, market={"venue": "kalshi", "id": "MK-2", "question": "Q2"})
    asyncio.run(po.poll_once())
    assert peak == 2


def test_working_live_orders_count_against_spent_today(tmp_ledger):
    po, _ = _orders(tmp_ledger, Feed(BOOK0), Clock())
    before = tmp_ledger.spent_today()
    row = _place(po, book_name="live")
    assert tmp_ledger.spent_today() == pytest.approx(before + row["requested"] * row["limit_price"])
    _place(po, book_name="shadow", market={"venue": "kalshi", "id": "MK-9", "question": "Q9"})
    assert tmp_ledger.spent_today() == pytest.approx(before + row["requested"] * row["limit_price"])   # side books do not count
    po.withdraw_all("test")
    assert tmp_ledger.spent_today() == pytest.approx(before)
