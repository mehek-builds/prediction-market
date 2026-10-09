"""Replay the last hours of real-rule signals against the live-quote selection rule. Read only.

    python3 -m fastlane.replay --since-hours 24 [--ledger PATH] [--universe PATH]

The ledger does not store the eight candidate books the engine saw, so this reconstructs what it can: the chosen market's
book at decision (`marks` horizon 0), the Kalshi `ticks` of tracked markets, the marks of the shadow and starter books
and the stored Jev `answers`. For every real-rule signal that did not trade it says whether the chosen market would now
be dropped before selection (live entry at 95c or more), whether another qualifier had room on the tape, or whether the
data to say is missing. It never claims a P&L.
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from fastlane.books import MAX_ENTRY_PRICE
from fastlane.config import load_env
from fastlane.decision import DECISIVE_MIN, SIGNAL_THRESHOLD, decide
from fastlane.ledger import DB_PATH, columns, mark_key
from fastlane.universe import CACHE, Universe, is_sports_market

TICK_WINDOW_S = 120          # a tick older than this before the decision says nothing about the book at decision
FILLED_REASONS = ("signal_yes", "signal_no", "post_working")   # not blocked: a fill or a resting order was placed
HEADER = ("Replay of stored real-rule signals. The eight candidate books are not stored, so alternatives are priced "
          "from marks and ticks only; this never claims a P&L.")


def load_universe(path: Path | None = None) -> Universe:
    """The universe from its cache file only (any age, no network)."""
    u = Universe()
    p = Path(path) if path else CACHE
    if p.exists():
        cached = json.loads(p.read_text())
        u._set([m for m in cached if m.get("venue") != "polymarket" or not is_sports_market(m)])
    return u


def _scores(answers: dict) -> dict[str, dict]:
    """{key: {side, strength, decisive}} computed exactly as decision.decide() scores a candidate."""
    out = {}
    for key, a in answers.items():
        p = a.get("probabilities") or {}
        yes = p.get("decisive_yes", 0) + p.get("toward_yes", 0)
        no = p.get("decisive_no", 0) + p.get("toward_no", 0)
        side = "yes" if yes >= no else "no"
        out[key] = {"side": side, "strength": max(yes, no), "decisive": p.get(f"decisive_{side}", 0)}
    return out


def _entry(side: str, yes_ask, yes_bid):
    if side == "yes":
        return yes_ask if yes_ask else None
    return round(1 - yes_bid, 4) if yes_bid else None


def _tick_entry(db: sqlite3.Connection, market_id: str, side: str, ts: float):
    row = db.execute("SELECT yes_bid, yes_ask FROM ticks WHERE market_id = ? AND ts <= ? AND ts >= ? "
                     "ORDER BY ts DESC LIMIT 1", (market_id, ts, ts - TICK_WINDOW_S)).fetchone()
    return _entry(side, row[1], row[0]) if row else None


def _mark_entry(db: sqlite3.Connection, key: str, side: str):
    row = db.execute("SELECT yes_ask, yes_bid FROM marks WHERE event_id = ? AND horizon_s = 0", (key,)).fetchone()
    return _entry(side, row[0], row[1]) if row else None


def analyse(db: sqlite3.Connection, since_ts: float, universe: Universe) -> tuple[list[str], dict]:
    have = columns(db, "decisions")
    extra = ", ".join(f"d.{c}" if c in have else f"NULL AS {c}" for c in ("shadow_market_id", "starter_market_id"))
    rows = db.execute(f"""
        SELECT e.id, e.headline, e.summary, d.decided_ts, d.action, d.reason, d.market_id, d.venue, d.answers,
               d.p_up, d.p_down, {extra}
        FROM events e JOIN decisions d ON d.event_id = e.id
        WHERE e.synthetic = 0 AND d.decided_ts >= ? AND d.answers IS NOT NULL
        ORDER BY d.decided_ts""", (since_ts,)).fetchall()
    lines: list[str] = []
    by_reason: Counter = Counter()
    s = {"qualified": 0, "blocked": 0, "by_reason": by_reason, "dropped": 0, "alt_room": 0, "unknown": 0, "kept": 0}
    for (eid, headline, summary, decided_ts, action, reason, market_id, venue, raw, p_up, p_down,
         shadow_mid, starter_mid) in rows:
        try:
            answers = json.loads(raw)
        except (TypeError, ValueError):
            continue
        scored = _scores(answers)
        qual = {k: v for k, v in scored.items() if v["strength"] >= SIGNAL_THRESHOLD and v["decisive"] >= DECISIVE_MIN}
        if not qual:
            continue
        s["qualified"] += 1
        if reason in FILLED_REASONS:
            continue
        s["blocked"] += 1
        by_reason[reason] += 1
        label = f"{time.strftime('%m-%d %H:%M:%S', time.localtime(decided_ts))} {reason:20} {headline[:56]}"
        side = "yes" if (p_up or 0) >= (p_down or 0) else "no"
        chosen_entry = _mark_entry(db, eid, side) if market_id else None

        # Rebuild the shortlist for the stored headline and check it still maps onto what the engine saw.
        cands = universe.shortlist(f"{headline} {summary or ''}")
        d = decide(answers)
        top = d.get("key")
        drifted = not (top and market_id and top[1:].isdigit() and int(top[1:]) < len(cands)
                       and cands[int(top[1:])]["id"] == market_id)
        if drifted:
            s["unknown"] += 1
            lines.append(f"{label} | shortlist_drifted (unknown)")
            continue
        if chosen_entry is None:
            s["unknown"] += 1
            lines.append(f"{label} | {market_id}: no stored book at decision (unknown)")
            continue
        kept = chosen_entry < MAX_ENTRY_PRICE
        alts = []
        for key, v in qual.items():
            idx = int(key[1:]) if key[1:].isdigit() else None
            if idx is None or idx >= len(cands) or cands[idx]["id"] == market_id:
                continue
            mk = cands[idx]["id"]
            px = _tick_entry(db, mk, v["side"], decided_ts) if cands[idx]["venue"] == "kalshi" else None
            if px is None and mk == shadow_mid:
                px = _mark_entry(db, mark_key(eid, "shadow"), v["side"])
            if px is None and mk == starter_mid:
                px = _mark_entry(db, mark_key(eid, "starter"), v["side"])
            alts.append((mk, px))
        room = [(mk, px) for mk, px in alts if px is not None and px < MAX_ENTRY_PRICE]
        alt_txt = ", ".join(f"{mk} {px if px is not None else '?'}" for mk, px in alts) or "none"
        if kept:
            s["kept"] += 1
            verdict = "chosen market still has room"
        elif room:
            s["alt_room"] += 1
            verdict = f"chosen dropped, another qualifier had room ({room[0][0]} at {room[0][1]})"
        else:
            s["dropped"] += 1
            verdict = "chosen would now be dropped"
        lines.append(f"{label} | {market_id} live entry {chosen_entry:.2f} | alts: {alt_txt} | {verdict}")
    return lines, s


def summary_line(s: dict) -> str:
    why = ", ".join(f"{k} {v}" for k, v in sorted(s["by_reason"].items())) or "none"
    return (f"qualified {s['qualified']} | blocked {s['blocked']} ({why}) | chosen would now be dropped {s['dropped']} "
            f"| another qualifier had room per tape {s['alt_room']} | unknown {s['unknown']} "
            f"| chosen still had room {s['kept']}")


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description="Replay stored real-rule signals against live-quote selection (read only)")
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--ledger", type=Path, default=DB_PATH, help="ledger file (a backup copy; never write to it)")
    ap.add_argument("--universe", type=Path, default=None, help="universe cache snapshot (default: the current cache)")
    a = ap.parse_args(argv)
    if not a.ledger.exists():
        out(f"No ledger at {a.ledger}")
        return 1
    db = sqlite3.connect(f"file:{quote(str(a.ledger))}?mode=ro", uri=True)
    try:
        out(HEADER)
        out(f"ledger {a.ledger}, last {a.since_hours:g}h, universe {a.universe or CACHE}\n")
        lines, s = analyse(db, time.time() - a.since_hours * 3600, load_universe(a.universe))
        for ln in lines:
            out(ln)
        out("\n" + summary_line(s))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    load_env()
    sys.exit(main())
