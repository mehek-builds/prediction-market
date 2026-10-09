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
