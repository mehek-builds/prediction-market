"""Live order books from Kalshi and Polymarket, normalised to ask ladders, plus a book-walking paper fill."""
import math
import time
from dataclasses import dataclass, field

import httpx

from fastlane.config import kalshi_api_url

POLY_CLOB = "https://clob.polymarket.com"
MAX_SLIPPAGE = 0.03     # never pay more than best ask + 3 cents
MAX_ENTRY_PRICE = 0.95  # above this there is almost nothing left to win
MAX_SPREAD = 0.03         # skip if getting out costs more than 3 cents of spread (env MAX_SPREAD_CENTS)
COST_TO_ROOM_MAX = 0.25   # skip if round-trip cost (spread + both taker fees) eats over a quarter of the room
                          # to profit (env COST_TO_ROOM_MAX). "If the cost of trading eats the gains, it's not worth it."
MIN_ENTRY_PRICE = 0.03    # below this a contract is a long shot: tiny room to lose, no realistic exit (env MIN_ENTRY_PRICE)
TICK = 0.01               # price step of a resting paper bid (Kalshi linear-cent markets; Polymarket quotes finer, we stay on cents)
COST_BLOCK_REASONS = frozenset({"too_expensive", "no_exit_liquidity", "longshot"})  # market-property blocks: recorded as PASS


@dataclass
class Book:
    venue: str
    market_id: str
    yes_asks: list = field(default_factory=list)  # [(price, qty)] ascending price
    no_asks: list = field(default_factory=list)
    fetched_at: float = 0.0
    fetch_ms: float = 0.0

    def best(self, side: str):
        asks = self.yes_asks if side == "yes" else self.no_asks
        return asks[0][0] if asks else None

    def bid(self, side: str):
        """Best price we could sell `side` at: 1 - best ask of the other side."""
        other = self.no_asks if side == "yes" else self.yes_asks
        return round(1 - other[0][0], 4) if other else None

    def mid(self):
        ya, yb = self.best("yes"), self.bid("yes")
        return round((ya + yb) / 2, 4) if ya is not None and yb is not None else None


async def fetch_book(client: httpx.AsyncClient, market: dict) -> Book:
    t0 = time.perf_counter()
    if market["venue"] == "kalshi":
        r = await client.get(f"{kalshi_api_url()}/markets/{market['id']}/orderbook")
        r.raise_for_status()
        ob = r.json().get("orderbook_fp") or {}
        yes_bids = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
        no_bids = [(float(p), float(q)) for p, q in ob.get("no_dollars") or []]
        # Buying YES lifts NO bids at (1 - price), and vice versa.
        yes_asks = sorted((round(1 - p, 4), q) for p, q in no_bids)
        no_asks = sorted((round(1 - p, 4), q) for p, q in yes_bids)
    else:
        r = await client.get(f"{POLY_CLOB}/book", params={"token_id": market["yes_token"]})
        r.raise_for_status()
        ob = r.json()
        yes_asks = sorted((float(a["price"]), float(a["size"])) for a in ob.get("asks") or [])
        no_asks = sorted((round(1 - float(b["price"]), 4), float(b["size"])) for b in ob.get("bids") or [])
    return Book(market["venue"], market["id"], yes_asks, no_asks, time.time(), (time.perf_counter() - t0) * 1000)


def kalshi_taker_fee(contracts: float, price: float) -> float:
    return math.ceil(round(0.07 * contracts * price * (1 - price) * 100, 6)) / 100


def taker_fee_per_contract(venue: str, price: float) -> float:
    """Kalshi 0.07 * p * (1 - p) per contract, unrounded (the per-order cent rounding is applied at fill time); 0 elsewhere."""
    return 0.07 * price * (1 - price) if venue == "kalshi" else 0.0


def round_trip_cost(book: Book, side: str) -> dict | None:
    """What one contract of `side` costs to buy at the ask and sell back at the bid right now, after both taker fees.

    None when the book lacks an ask or a bid for that side.
    Keys: entry, exit_bid, spread, fee_in, fee_out, cost, room (room = 1 - entry).
    """
    entry, exit_bid = book.best(side), book.bid(side)
    if entry is None or exit_bid is None:
        return None
    spread = max(round(entry - exit_bid, 4), 0.0)   # a crossed book is not a negative cost
    room = round(1 - entry, 4)
    fee_in = taker_fee_per_contract(book.venue, entry)
    fee_out = taker_fee_per_contract(book.venue, exit_bid)
    return {"entry": entry, "exit_bid": exit_bid, "spread": spread, "fee_in": fee_in, "fee_out": fee_out,
            "cost": round(spread + fee_in + fee_out, 6), "room": room}


def cost_block(book: Book, side: str, max_spread: float = MAX_SPREAD,
               cost_to_room_max: float = COST_TO_ROOM_MAX, min_entry: float = MIN_ENTRY_PRICE) -> str | None:
    """'no_exit_liquidity' | 'longshot' | 'too_expensive' | None, in that order of precedence.

    no_exit_liquidity: the held side has no bid, or a bid of 0 (nobody to sell to, ever).
    longshot: entry below min_entry.
    too_expensive: spread > max_spread, or round-trip cost > cost_to_room_max * room.
    None also when the held side has no ask at all (the fill guard reports no_fill_within_limit).
    """
    entry = book.best(side)
    if entry is None:
        return None  # the fill guard handles empty ladders
    exit_bid = book.bid(side)
    if exit_bid is None or exit_bid <= 0:
        return "no_exit_liquidity"
    if entry < round(min_entry, 4):
        return "longshot"
    c = round_trip_cost(book, side)
    if c is None:
        return None
    if c["spread"] > round(max_spread, 4) or c["cost"] > round(cost_to_room_max * c["room"], 6):
        return "too_expensive"
    return None


def simulate_fill(book: Book, side: str, budget_usd: float) -> dict | None:
    """Walk the ask ladder like a marketable limit order capped at best + MAX_SLIPPAGE."""
    asks = book.yes_asks if side == "yes" else book.no_asks
    if not asks:
        return None
    best = asks[0][0]
    if best > MAX_ENTRY_PRICE:
        return None
    limit = min(best + MAX_SLIPPAGE, MAX_ENTRY_PRICE)
    contracts, cost = 0.0, 0.0
    for price, qty in asks:
        if price > limit or cost >= budget_usd:
            break
        take = min(qty, (budget_usd - cost) / price)
        if book.venue == "kalshi":
            take = math.floor(take)  # whole contracts only
        if take <= 0:
            break
        contracts += take
        cost += take * price
    if contracts <= 0:
        return None
    avg = cost / contracts
    fee = kalshi_taker_fee(contracts, avg) if book.venue == "kalshi" else 0.0
    return {"contracts": round(contracts, 2), "avg_price": round(avg, 4), "cost": round(cost, 2),
            "fee": fee, "best_ask": best, "limit": round(limit, 4)}


def post_limit(book: Book, side: str, min_entry: float = MIN_ENTRY_PRICE) -> float | None:
    """Price for a resting buy of `side`: best bid of that side + TICK, but never at or above the best ask (if the spread
    is one tick, join the bid: L = bid). None when the side has no bid, or when the price would be at or above
    MAX_ENTRY_PRICE (no room) or below `min_entry` (long shot): the caller reports no_room / longshot."""
    bid = book.bid(side)
    if bid is None or bid <= 0:
        return None
    ask = book.best(side)
    limit = round(bid + TICK, 2)
    if ask is not None and limit >= ask - 1e-9:
        limit = round(bid, 2)
    if limit >= MAX_ENTRY_PRICE - 1e-9 or limit < round(min_entry, 4) - 1e-9:
        return None
    return limit


def post_size(venue: str, limit: float, budget_usd: float) -> float:
    """Contracts for a resting order: budget / limit. Whole contracts on Kalshi (floor, 0 when under one), floored to two
    decimals on Polymarket (never above the budget)."""
    if limit <= 0:
        return 0.0
    n = budget_usd / limit
    if venue == "kalshi":
        return float(math.floor(n + 1e-9))
    return math.floor(n * 100 + 1e-9) / 100


def post_fill(side: str, limit: float, remaining: float, book: Book, venue: str) -> dict | None:
    """Fill model for a resting bid (conservative, we are last in the queue at our price): a fill happens only when the
    OPPOSITE side comes to us, i.e. the best ask of `side` in `book` is at or below `limit`. Quantity is the smaller of
    `remaining` and the displayed size at ask prices <= limit (whole contracts on Kalshi). The price is our limit,
    never better. None when nothing fills. Returns {"contracts", "price", "ask_seen", "qty_seen"}."""
    asks = book.yes_asks if side == "yes" else book.no_asks
    reachable = [(p, q) for p, q in asks if p <= limit + 1e-9]
    if not reachable:
        return None
    qty_seen = sum(q for _, q in reachable)
    n = min(remaining, qty_seen)
    n = float(math.floor(n + 1e-9)) if venue == "kalshi" else math.floor(n * 100 + 1e-9) / 100
    if n <= 0:
        return None
    return {"contracts": n, "price": round(limit, 4), "ask_seen": reachable[0][0], "qty_seen": qty_seen}
