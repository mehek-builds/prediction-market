"""Live order books from Kalshi and Polymarket, normalised to ask ladders, plus a book-walking paper fill."""
import math
import time
from dataclasses import dataclass, field

import httpx

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_CLOB = "https://clob.polymarket.com"
MAX_SLIPPAGE = 0.03     # never pay more than best ask + 3 cents
MAX_ENTRY_PRICE = 0.95  # above this there is almost nothing left to win


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
        r = await client.get(f"{KALSHI}/markets/{market['id']}/orderbook")
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
