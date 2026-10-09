import pytest

from fastlane.books import kalshi_taker_fee, simulate_fill


def test_ladder_walk(book_factory):
    b = book_factory("kalshi", [(.40, 10), (.42, 10), (.50, 100)])
    f = simulate_fill(b, "yes", 100)
    assert f["contracts"] == 20 and f["cost"] == 8.2 and f["avg_price"] == .41
    assert f["limit"] == .43 and f["fee"] == pytest.approx(.34)


def test_budget_cap(book_factory):
    f = simulate_fill(book_factory("kalshi", [(.50, 1000)]), "yes", 100)
    assert f["contracts"] == 200 and f["cost"] == 100


def test_whole_contracts_kalshi_vs_fractional_poly(book_factory):
    k = simulate_fill(book_factory("kalshi", [(.33, 1000)]), "yes", 10)
    assert k["contracts"] == 30 and k["cost"] == 9.9
    p = simulate_fill(book_factory("polymarket", [(.33, 1000)]), "yes", 10)
    assert p["contracts"] == 30.3


def test_max_entry_blocks(book_factory):
    assert simulate_fill(book_factory("kalshi", [(.96, 100)]), "yes", 100) is None


def test_limit_capped_at_max_entry(book_factory):
    f = simulate_fill(book_factory("kalshi", [(.94, 1), (.96, 100)]), "yes", 100)
    assert f["contracts"] == 1


def test_empty_asks_and_poly_fee(book_factory):
    assert simulate_fill(book_factory("kalshi", [], []), "yes", 100) is None
    assert simulate_fill(book_factory("polymarket", [(.4, 10)]), "yes", 100)["fee"] == 0.0


def test_no_side_walks_no_asks(book_factory):
    f = simulate_fill(book_factory("kalshi", [(.9, 10)], [(.3, 10)]), "no", 3)
    assert f["contracts"] == 10 and f["avg_price"] == .3


def test_fees():
    assert kalshi_taker_fee(100, .5) == 1.75
    assert kalshi_taker_fee(1, .5) == 0.02
    assert kalshi_taker_fee(10, .99) == 0.01


def test_bid_and_mid(book_factory):
    b = book_factory("kalshi", [(.60, 1)], [(.45, 1)])
    assert b.bid("yes") == .55
    assert b.mid() == .575
    assert book_factory("kalshi", [(.6, 1)], []).mid() is None


from fastlane.books import cost_block, round_trip_cost, taker_fee_per_contract  # noqa: E402


def test_fee_per_contract():
    assert taker_fee_per_contract("kalshi", .5) == pytest.approx(.0175)
    assert taker_fee_per_contract("polymarket", .5) == 0.0


def test_round_trip_cost_fields(book_factory):
    b = book_factory("kalshi", yes_asks=[(.55, 10)], no_asks=[(.47, 10)])   # yes bid = .53
    c = round_trip_cost(b, "yes")
    assert (c["entry"], c["exit_bid"], c["spread"], c["room"]) == (.55, .53, .02, .45)
    assert c["fee_in"] == pytest.approx(.07 * .55 * .45) and c["fee_out"] == pytest.approx(.07 * .53 * .47)
    assert c["cost"] == pytest.approx(.02 + .017325 + .017437)
    assert round_trip_cost(book_factory("kalshi", yes_asks=[(.55, 10)]), "yes") is None   # no bid


def test_spread_boundary_exactly_three_cents(book_factory):
    at = book_factory("polymarket", yes_asks=[(.40, 10)], no_asks=[(.63, 10)])    # bid .37, spread .03: allowed
    assert cost_block(at, "yes") is None
    over = book_factory("polymarket", yes_asks=[(.40, 10)], no_asks=[(.64, 10)])  # spread .04: blocked
    assert cost_block(over, "yes") == "too_expensive"
    assert cost_block(over, "yes", max_spread=.04) is None                        # env-configurable


def test_cost_to_room_boundary_exactly_quarter(book_factory):
    at = book_factory("polymarket", yes_asks=[(.88, 10)], no_asks=[(.15, 10)])
    assert cost_block(at, "yes") is None
    over = book_factory("polymarket", yes_asks=[(.89, 10)], no_asks=[(.14, 10)])
    assert cost_block(over, "yes") == "too_expensive"
    assert cost_block(over, "yes", cost_to_room_max=.5) is None


def test_kalshi_fee_counts_polymarket_does_not(book_factory):
    poly = book_factory("polymarket", yes_asks=[(.90, 10)], no_asks=[(.12, 10)])
    kal = book_factory("kalshi", yes_asks=[(.90, 10)], no_asks=[(.12, 10)])
    assert cost_block(poly, "yes") is None and cost_block(kal, "yes") == "too_expensive"


def test_cost_block_no_side(book_factory):
    b = book_factory("kalshi", yes_asks=[(.60, 10)], no_asks=[(.42, 10)])    # NO entry .42, NO bid = 1 - .60 = .40
    c = round_trip_cost(b, "no")
    assert (c["entry"], c["exit_bid"], c["spread"]) == (.42, .40, .02) and cost_block(b, "no") is None
    assert cost_block(book_factory("kalshi", no_asks=[(.42, 10)]), "no") is None   # no bid: left to the fill guard


def test_cost_block_empty_book_abstains(book_factory):
    assert cost_block(book_factory("kalshi"), "yes") is None


def test_crossed_book_spread_is_clamped_to_zero(book_factory):
    b = book_factory("kalshi", yes_asks=[(.50, 10)], no_asks=[(.45, 10)])   # yes bid .55 > yes ask .50
    c = round_trip_cost(b, "yes")
    assert c["spread"] == 0.0 and c["cost"] >= 0
