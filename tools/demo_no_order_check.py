"""One-off check that a NO order really opens a NO position, on Kalshi's DEMO environment only (fake money).

The bot buys NO by sending a Create Order (V2) `ask` on the YES book (selling YES at 1 - L, where L is the NO limit).
The open question is whether that opens a NO position when you hold no YES. Only a fill and a look at the position can
answer it, so there are two modes. Both refuse to run unless KALSHI_BASE_URL is the demo host, both send nothing
without --yes, and both need a DEMO API key in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (a production key does
not work on demo).

  accept-only (default): one 1-contract NO buy at a NO limit of 1 to 5 cents, immediate-or-cancel. A NO limit of p
      cents is a YES ask at (100 - p) cents, and it matches any YES bid at or above that, so this price only fills
      against a YES bid of 95 cents or more. On an ordinary market it does not fill: it proves the body is accepted
      (HTTP 2xx) and nothing else. It does NOT prove a NO position opens.
  --fill: places ONE 1-contract NO buy at a marketable price (100 minus the best YES bid, so it takes that bid),
      then reads the signed GET /portfolio/positions for the ticker and passes only if the position is +1 NO
      (-1 YES in Kalshi's signed convention). The raw positions JSON is always printed. --close then buys the one
      YES contract back (a second order through the same single call site) so the demo account is flat again.

    KALSHI_BASE_URL=https://demo-api.kalshi.co python3 tools/demo_no_order_check.py --ticker <demo ticker> --fill --yes
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastlane.config import KALSHI_DEMO_BASE, kalshi_base_url, load_env  # noqa: E402

POSITIONS_PATH = "/trade-api/v2/portfolio/positions"
BOOK_PATH = "/trade-api/v2/markets/{ticker}/orderbook"


def build_body(ticker: str, price_cents: int, client_order_id: str, side: str = "no") -> dict:
    from fastlane import live
    return live.order_body(ticker, side, 1, price_cents / 100, client_order_id)


def _client():
    from fastlane.kalshi import KalshiClient
    k = KalshiClient()
    if not k.configured:
        raise SystemExit("set a DEMO KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    return k


def _json(r) -> dict:
    try:
        out = r.json()
    except ValueError:
        return {"raw": r.text[:500]}
    return out if isinstance(out, dict) else {"raw": out}


def place_one(base: str, ticker: str, price_cents: int, side: str = "no") -> dict:
    """The only order call in this script: one 1-contract immediate-or-cancel order, signed for ORDER_PATH."""
    from fastlane import live
    k = _client()
    body = build_body(ticker, price_cents, "demo-" + side + "-" + uuid.uuid4().hex[:12], side)
    r = httpx.post(base + live.ORDER_PATH, json=body, headers=k.sign_headers("POST", live.ORDER_PATH), timeout=20)
    return {"http_status": r.status_code, "request": body, "response": _json(r)}


def signed_get(base: str, path: str, params: dict | None = None) -> dict:
    k = _client()
    r = httpx.get(base + path, params=params, headers=k.sign_headers("GET", path), timeout=20)
    return {"http_status": r.status_code, "response": _json(r)}


def _cents(v) -> int | None:
    """A price as whole cents from either a cents integer (63) or a dollar string ("0.6300")."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f * 100)) if f <= 1 and not float(f).is_integer() else int(round(f))


def best_bids(payload: dict) -> tuple[int | None, int | None]:
    """(best YES bid, best NO bid) in cents from an orderbook response. Parsed defensively: the levels may sit under
    `orderbook` or `orderbook_fp`, as [price, qty] pairs, and prices may be cents or dollar strings."""
    book = payload.get("orderbook") or payload.get("orderbook_fp") or payload
    out = []
    for side in ("yes", "no"):
        levels = book.get(side) or book.get(side + "_dollars") or []
        prices = [_cents(l[0]) for l in levels if isinstance(l, (list, tuple)) and l]
        prices = [p for p in prices if p is not None and 0 < p < 100]
        out.append(max(prices) if prices else None)
    return out[0], out[1]


def position_of(payload: dict, ticker: str) -> float | None:
    """Signed position for the ticker (Kalshi: positive = YES contracts, negative = NO contracts), 0 if the market is
    absent from the response, None if the response has no recognisable shape."""
    rows = payload.get("market_positions")
    if rows is None:
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("ticker") == ticker:
            for key in ("position", "position_fp"):
                if row.get(key) is not None:
                    try:
                        return float(row[key])
                    except (TypeError, ValueError):
                        return None
            return None
    return 0.0


def verdict(pos: float | None) -> str:
    if pos is None:
        return "UNKNOWN (unrecognised positions response, read the raw JSON)"
    if pos == -1:
        return "PASS: position is +1 NO (-1 YES)"
    return f"FAIL: position is {pos:g} (YES positive, NO negative); expected -1"


def run_fill(base: str, ticker: str, close: bool, save: str | None) -> int:
    book = signed_get(base, BOOK_PATH.format(ticker=ticker))
    yes_bid, no_bid = best_bids(book["response"])
    if yes_bid is None:
        print("refusing: no YES bid on this demo market, pick a liquid one")
        print(json.dumps(book, indent=1))
        return 1
    before = signed_get(base, POSITIONS_PATH, {"ticker": ticker})
    if position_of(before["response"], ticker) not in (0.0,):
        print("refusing: you already hold a position in this market (or the response is unreadable); pick a flat one")
        print(json.dumps(before, indent=1))
        return 1
    no_price = 100 - yes_bid     # NO limit L is a YES ask at 100 - L = the best YES bid, so it takes that bid
    order = place_one(base, ticker, no_price, "no")
    after = signed_get(base, POSITIONS_PATH, {"ticker": ticker})
    pos = position_of(after["response"], ticker)
    out = {"order": order, "positions_after": after, "no_limit_cents": no_price, "verdict": verdict(pos)}
    print(json.dumps(out, indent=1))
    ok = order["http_status"] < 300 and pos == -1
    if close and pos != -1:
        print(f"not closing: the position is {pos}, not exactly -1. Close it by hand on the demo site.")
    elif close:
        yes_price = 100 - no_bid if no_bid is not None else yes_bid + 1   # YES buy lifts the YES ask
        out["close_order"] = place_one(base, ticker, yes_price, "yes")
        out["positions_after_close"] = signed_get(base, POSITIONS_PATH, {"ticker": ticker})
        print(json.dumps({k: out[k] for k in ("close_order", "positions_after_close")}, indent=1))
    if save:
        Path(save).write_text(json.dumps(out, indent=1) + "\n")
    print(out["verdict"])
    return 0 if ok else 1


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticker", required=True, help="a market ticker that exists on the demo exchange")
    ap.add_argument("--fill", action="store_true", help="place one marketable NO buy and verify the position")
    ap.add_argument("--close", action="store_true", help="with --fill: buy the YES contract back afterwards")
    ap.add_argument("--price-cents", type=int, default=1,
                    help="accept-only mode: NO limit in cents, 1 to 5 (default 1; fills only against a YES bid of 95c+)")
    ap.add_argument("--save", metavar="FILE", help="write the request and response JSON here (a test fixture)")
    ap.add_argument("--yes", action="store_true", help="actually send the order(s)")
    a = ap.parse_args(argv)
    try:
        base = kalshi_base_url()
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if base != KALSHI_DEMO_BASE:
        print(f"refusing: KALSHI_BASE_URL is {base}, this script only runs against {KALSHI_DEMO_BASE}")
        return 2
    if a.fill:
        if not a.yes:
            print("dry run, nothing sent. --fill would read the demo order book, then send ONE 1-contract NO buy at")
            print("100 minus the best YES bid, read the positions, and pass only if the position is +1 NO. Body shape:")
            print(json.dumps(build_body(a.ticker, 50, "demo-no-dryrun"), indent=1))
            return 0
        return run_fill(base, a.ticker, a.close, a.save)
    if not 1 <= a.price_cents <= 5:
        print("refusing: accept-only mode takes 1 to 5 cents (use --fill for a marketable price)")
        return 2
    if not a.yes:
        print("dry run, nothing sent. Body would be:")
        print(json.dumps(build_body(a.ticker, a.price_cents, "demo-no-dryrun"), indent=1))
        return 0
    out = place_one(base, a.ticker, a.price_cents)
    print(json.dumps(out, indent=1))
    print("accepted only: this proves the body is well formed, not that a NO position opens. Run --fill.")
    if a.save:
        Path(a.save).write_text(json.dumps(out, indent=1) + "\n")
    return 0 if out["http_status"] < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
