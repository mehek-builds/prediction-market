"""The Jev question set and the fixed trade rule. Shared by the live engine and the benchmark.

One question per candidate market, all answered in parallel inside a single Jev call. Jev's questions cannot see
each other's answers, so a separate "which market?" question cannot tell a "direction?" question which market it
picked. Naming the market inside each question removes that ambiguity (e.g. "Fed holds" is YES for the
"no change" market and NO for the "cut 25 bps" market).
"""

import os

# MAX_ENTRY_PRICE (0.95) is the fill guard's cap, reused so "no room" means "would not fill"
from fastlane.books import COST_TO_ROOM_MAX, MAX_ENTRY_PRICE, MAX_SPREAD, MIN_ENTRY_PRICE

# Fixed decision threshold: the trade rule lives here, not in the model.
SIGNAL_THRESHOLD = 0.85   # probability mass on (decisive + toward) in one direction
DECISIVE_MIN = 0.30       # ...and at least this much on "decisive". Lean-only news is tracked (marks), not traded,
                          # until the ledger shows lean-only signals actually move prices.
MARK_THRESHOLD = 0.30     # below this we do not even track the market afterwards

# Shadow rule: what a looser rule WOULD have traded, recorded in a separate paper book (never the real one).
# Env-configurable so thresholds can be explored without touching the real rule above.
SHADOW_SIGNAL_THRESHOLD = 0.60
SHADOW_DECISIVE_MIN = 0.0
SHADOW_ENABLED = True
BUCKET_EDGES = (0.60, 0.70, SIGNAL_THRESHOLD)  # shadow P&L is reported per strength bucket to find the cutoff

# Starter book: a third paper book with a small fixed size and a stricter strength bar than shadow, no decisive
# requirement. Env only (STARTER_*), never touches the real book or real orders.
STARTER_SIGNAL_THRESHOLD = 0.90
STARTER_DECISIVE_MIN = 0.0
STARTER_ENABLED = True
STARTER_SIZE_USD = 20.0

# How a paper entry is placed: "take" crosses the spread now (the fill the real IOC order mirrors), "post" rests a bid
# inside the spread and fills only if the opposite side comes to us (the orders module).
ENTRY_STYLES = ("take", "post")
ENTRY_STYLE_DEFAULTS = {"live": "take", "shadow": "post", "starter": "post"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def shadow_settings() -> tuple[bool, float, float]:
    """(enabled, signal_threshold, decisive_min) from SHADOW_ENABLED / SHADOW_SIGNAL_THRESHOLD / SHADOW_DECISIVE_MIN."""
    enabled = os.environ.get("SHADOW_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    return enabled, _env_float("SHADOW_SIGNAL_THRESHOLD", SHADOW_SIGNAL_THRESHOLD), _env_float("SHADOW_DECISIVE_MIN", SHADOW_DECISIVE_MIN)


def starter_settings() -> tuple[bool, float, float, float]:
    """(enabled, signal_threshold, decisive_min, size_usd) from STARTER_ENABLED / STARTER_SIGNAL_THRESHOLD /
    STARTER_DECISIVE_MIN / STARTER_SIZE_USD."""
    enabled = os.environ.get("STARTER_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    return (enabled, _env_float("STARTER_SIGNAL_THRESHOLD", STARTER_SIGNAL_THRESHOLD),
            _env_float("STARTER_DECISIVE_MIN", STARTER_DECISIVE_MIN), _env_float("STARTER_SIZE_USD", STARTER_SIZE_USD))


def entry_styles() -> dict[str, str]:
    """{"live": .., "shadow": .., "starter": ..}, each "take" or "post". Defaults live=take, shadow=post, starter=post.
    ENTRY_STYLE (if set) replaces all three; ENTRY_STYLE_LIVE / _SHADOW / _STARTER then override one book each.
    Values are lower-cased and stripped, empty means unset; anything else raises ValueError."""
    def read(name: str) -> str | None:
        raw = os.environ.get(name, "").strip().lower()
        if not raw:
            return None
        if raw not in ENTRY_STYLES:
            raise ValueError(f"{name} must be one of {', '.join(ENTRY_STYLES)} (got {raw!r})")
        return raw

    global_style = read("ENTRY_STYLE")
    out = {book: global_style or default for book, default in ENTRY_STYLE_DEFAULTS.items()}
    for book in out:
        out[book] = read(f"ENTRY_STYLE_{book.upper()}") or out[book]
    return out


def cost_settings() -> tuple[float, float, float]:
    """(max_spread, cost_to_room_max, min_entry) from MAX_SPREAD_CENTS (cents) / COST_TO_ROOM_MAX (ratio) /
    MIN_ENTRY_PRICE (a price, not cents)."""
    return (_env_float("MAX_SPREAD_CENTS", MAX_SPREAD * 100) / 100, _env_float("COST_TO_ROOM_MAX", COST_TO_ROOM_MAX),
            _env_float("MIN_ENTRY_PRICE", MIN_ENTRY_PRICE))


def strength_bucket(strength: float | None) -> str | None:
    """'0.60-0.70' | '0.70-0.85' | '0.85+' | '<0.60' (lower edge inclusive); None when strength is unknown."""
    if strength is None:
        return None
    edges = BUCKET_EDGES
    if strength < edges[0]:
        return f"<{edges[0]:.2f}"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= strength < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return f"{edges[-1]:.2f}+"

LEVELS = {
    "decisive_yes": "The headline effectively confirms this market will resolve YES.",
    "toward_yes": "The headline is significant new information that makes YES clearly more likely.",
    "no_signal": "The headline is unrelated to this market, or only weakly relevant to it.",
    "toward_no": "The headline is significant new information that makes YES clearly less likely.",
    "decisive_no": "The headline effectively confirms this market will resolve NO.",
}


def build_request(item: dict, candidates: list[dict]):
    keyed = {f"m{i}": m for i, m in enumerate(candidates)}
    state = {"headline": item["headline"], "summary": item.get("summary", ""), "source": item.get("source", "")}
    questions = {
        k: {
            "type": "choice",
            "instructions": f'Prediction market: "{m["question"]}". '
                            "How does this headline change the chance that this market resolves YES?",
            "criteria": LEVELS,
        }
        for k, m in keyed.items()
    }
    return state, questions, keyed


def _qualifies(c: dict, signal_threshold: float = SIGNAL_THRESHOLD, decisive_min: float = DECISIVE_MIN) -> bool:
    return c["strength"] >= signal_threshold and c["p_decisive"] >= decisive_min


def decide(answers: dict, keyed: dict | None = None, *, quotes: dict | None = None,
           signal_threshold: float = SIGNAL_THRESHOLD, decisive_min: float = DECISIVE_MIN) -> dict:
    """Score every candidate, then pick the qualifying one with the most room to profit.

    Room = 1 - entry price on the signalled side. `quotes` ({key: {"yes_ask", "yes_bid"}} from live order books) REPLACE
    the cached `keyed` prices for the keys they cover, including a None (the live book has no price on that side);
    keys absent from `quotes` use the cache exactly as before. With a live quote and no entry on the signalled side the
    candidate has room 0.0 and `no_ask=True` (nothing to buy). With no usable cached quote room is 0.5 (unknown).
    Qualifiers with no room (entry at or above MAX_ENTRY_PRICE, so the fill guard would refuse them anyway) are dropped; if
    only such qualifiers exist the answer is PASS / priced_in (PASS / no_fill_within_limit when the strongest has
    nothing to buy) and the market is still tracked.
    Without qualifiers, report the strongest candidate so it can still be tracked for calibration.
    Thresholds are parameters so the shadow and starter rules (looser, separate paper books) can reuse the exact same
    scoring, room ranking and no-room fallback; the defaults are the real rule. The cost filter is a separate guard on
    the live book (`books.cost_block`), applied by the engine after selection.
    The result also carries `entry` (float | None) and `quote_source` ("live" | "cache" | "none") for the chosen key.
    Pure: no I/O, no clock.
    """
    keyed = keyed or {}
    quotes = quotes or {}
    cands = []
    for key, a in answers.items():
        p = a.get("probabilities") or {}
        yes = p.get("decisive_yes", 0) + p.get("toward_yes", 0)
        no = p.get("decisive_no", 0) + p.get("toward_no", 0)
        side = "yes" if yes >= no else "no"
        if key in quotes:
            src, q = "live", quotes[key] or {}
        else:
            src, q = "cache", keyed.get(key, {})
        entry = q.get("yes_ask") if side == "yes" else (1 - q["yes_bid"] if q.get("yes_bid") else None)
        entry = entry or None
        cand = {"key": key, "side": side, "p_yes_side": yes, "p_no_side": no,
                "p_decisive": p.get(f"decisive_{side}", 0), "strength": max(yes, no),
                "room": (1 - entry) if entry else 0.5, "entry": entry,
                "quote_source": src if (entry or src == "live") else "none"}
        if src == "live" and not entry:
            cand["room"], cand["no_ask"] = 0.0, True
        cands.append(cand)
    if not cands:
        return {"action": "PASS", "reason": "no_answers", "key": None}
    signalled = [c for c in cands if _qualifies(c, signal_threshold, decisive_min)]
    qualified = [c for c in signalled if c["room"] > 1 - MAX_ENTRY_PRICE]
    if qualified:
        best = max(qualified, key=lambda c: (c["room"], c["strength"]))
        return {**best, "action": "BUY_YES" if best["side"] == "yes" else "BUY_NO", "reason": f"signal_{best['side']}"}
    if signalled:  # a correct call on a market already priced near certainty: nothing left to win, track it only
        best = max(signalled, key=lambda c: c["strength"])
        return {**best, "action": "PASS", "reason": "no_fill_within_limit" if best.get("no_ask") else "priced_in"}
    best = max(cands, key=lambda c: c["strength"])
    if best["strength"] < MARK_THRESHOLD:
        return {**best, "action": "PASS", "reason": "irrelevant", "key": None}
    reason = "weak_signal" if best["strength"] < signal_threshold else "lean_not_decisive"
    return {**best, "action": "PASS", "reason": reason}
