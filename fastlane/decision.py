"""The Jev question set and the fixed trade rule. Shared by the live engine and the benchmark.

One question per candidate market, all answered in parallel inside a single Jev call. Jev's questions cannot see
each other's answers, so a separate "which market?" question cannot tell a "direction?" question which market it
picked. Naming the market inside each question removes that ambiguity (e.g. "Fed holds" is YES for the
"no change" market and NO for the "cut 25 bps" market).
"""

# Fixed decision threshold: the trade rule lives here, not in the model.
SIGNAL_THRESHOLD = 0.85   # probability mass on (decisive + toward) in one direction
DECISIVE_MIN = 0.30       # ...and at least this much on "decisive". Lean-only news is tracked (marks), not traded,
                          # until the ledger shows lean-only signals actually move prices.
MARK_THRESHOLD = 0.30     # below this we do not even track the market afterwards

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


def _qualifies(c: dict) -> bool:
    return c["strength"] >= SIGNAL_THRESHOLD and c["p_decisive"] >= DECISIVE_MIN


def decide(answers: dict, keyed: dict | None = None) -> dict:
    """Score every candidate, then pick the qualifying one with the most room to profit.

    Room = 1 - entry price on the signalled side, from the cached quote (the live book is used for the fill).
    Without qualifiers, report the strongest candidate so it can still be tracked for calibration.
    """
    keyed = keyed or {}
    cands = []
    for key, a in answers.items():
        p = a.get("probabilities") or {}
        yes = p.get("decisive_yes", 0) + p.get("toward_yes", 0)
        no = p.get("decisive_no", 0) + p.get("toward_no", 0)
        side = "yes" if yes >= no else "no"
        m = keyed.get(key, {})
        entry = m.get("yes_ask") if side == "yes" else (1 - m["yes_bid"] if m.get("yes_bid") else None)
        cands.append({"key": key, "side": side, "p_yes_side": yes, "p_no_side": no,
                      "p_decisive": p.get(f"decisive_{side}", 0), "strength": max(yes, no),
                      "room": (1 - entry) if entry else 0.5})
    if not cands:
        return {"action": "PASS", "reason": "no_answers", "key": None}
    qualified = [c for c in cands if _qualifies(c)]
    if qualified:
        best = max(qualified, key=lambda c: (c["room"], c["strength"]))
        return {**best, "action": "BUY_YES" if best["side"] == "yes" else "BUY_NO", "reason": f"signal_{best['side']}"}
    best = max(cands, key=lambda c: c["strength"])
    if best["strength"] < MARK_THRESHOLD:
        return {**best, "action": "PASS", "reason": "irrelevant", "key": None}
    reason = "weak_signal" if best["strength"] < SIGNAL_THRESHOLD else "lean_not_decisive"
    return {**best, "action": "PASS", "reason": reason}
