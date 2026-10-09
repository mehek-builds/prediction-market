"""Timeline + P&L report from the ledger.  python3 -m fastlane.report [--since-minutes 60]"""
import argparse
import sqlite3
import time
from collections import defaultdict

from fastlane.ledger import DB_PATH

HORIZONS = [0, 5, 30, 60, 300, 900, 3600]


def pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def row(label, vals, unit="ms"):
    vals = [v for v in vals if v is not None]
    if not vals:
        return f"  {label:34} n=0"
    f = (lambda v: f"{v:8.0f}{unit}") if unit == "ms" else (lambda v: f"{v:8.0f}{unit}")
    return (f"  {label:34} n={len(vals):<5} p50 {f(pct(vals, .5))}  p90 {f(pct(vals, .9))}  "
            f"p99 {f(pct(vals, .99))}  max {f(max(vals))}")


def main(since_minutes: float | None):
    if not DB_PATH.exists():
        print("No ledger yet. Run python3 -m fastlane.run first.")
        return
    db = sqlite3.connect(DB_PATH)
    since = time.time() - since_minutes * 60 if since_minutes else 0
    rows = db.execute("""
        SELECT e.id, e.source, e.headline, e.published_ts, e.seen_ts, e.synthetic,
               d.decided_ts, d.n_candidates, d.shortlist_ms, d.jev_ms, d.book_ms, d.total_ms,
               d.action, d.reason, d.market_id, d.market_question, d.p_up, d.p_down, d.materiality, d.market_conf
        FROM events e JOIN decisions d ON d.event_id = e.id
        WHERE e.seen_ts > ? AND e.synthetic = 0""", (since,)).fetchall()
    if not rows:
        print("No live decisions in range.")
        return
    print(f"\n=== FAST LANE REPORT: {len(rows)} live news events ===\n")

    print("Pipeline stages (our code, per event):")
    print(row("market match (shortlist)", [r[8] for r in rows]))
    print(row("Jev decision call", [r[9] for r in rows]))
    print(row("order-book wait after Jev", [r[10] for r in rows]))
    print(row("seen -> decision (total)", [r[11] for r in rows]))

    print("\nSource lag (published -> we saw it), seconds:")
    by_src = defaultdict(list)
    for r in rows:
        if r[3]:
            by_src[r[1]].append(r[4] - r[3])
    for src, lags in sorted(by_src.items(), key=lambda kv: pct(kv[1], .5)):
        print(row(src, lags, unit="s"))
    print(row("ALL SOURCES published -> decision", [r[6] - r[3] for r in rows if r[3]], unit="s"))

    print("\nDecisions:")
    counts = defaultdict(int)
    for r in rows:
        counts[(r[12], r[13])] += 1
    for (a, why), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {a:8} {why:24} {n}")

    # Did matched markets move after the news? (calibration for thresholds)
    marks = defaultdict(dict)
    for eid, h, ya, yb, mid in db.execute("SELECT event_id, horizon_s, yes_ask, yes_bid, mid FROM marks"):
        marks[eid][h] = (ya, yb, mid)
    moved = defaultdict(list)
    for r in rows:
        m = marks.get(r[0])
        if not m or 0 not in m or m[0][2] is None:
            continue
        for h in HORIZONS[1:]:
            if h in m and m[h][2] is not None:
                moved[h].append(abs(m[h][2] - m[0][2]) * 100)
    if moved:
        print("\nAbsolute mid-price move of the matched market after decision (cents):")
        for h in HORIZONS[1:]:
            if moved[h]:
                big = sum(1 for v in moved[h] if v >= 2)
                print(f"  +{h:>5}s  n={len(moved[h]):<4} p50 {pct(moved[h], .5):5.1f}c  p90 {pct(moved[h], .9):5.1f}c  "
                      f"max {max(moved[h]):5.1f}c  moved>=2c: {big}")
        print("  Largest movers (what we might be missing):")
        best = []
        for r in rows:
            m = marks.get(r[0])
            if m and 0 in m and m[0][2] is not None:
                later = [m[h][2] for h in HORIZONS[1:] if h in m and m[h][2] is not None]
                if later:
                    best.append((max(abs(x - m[0][2]) for x in later) * 100, r))
        for mv, r in sorted(best, key=lambda x: -x[0])[:5]:
            print(f"    {mv:4.1f}c {r[12]:7} up {r[16] or 0:.2f} down {r[17] or 0:.2f} mat {r[18] or 0:.2f} | "
                  f"{r[2][:60]}\n{'':10}-> {(r[15] or '')[:80]}")

    timeline(db, rows)

    trades = db.execute("""SELECT event_id, venue, market_question, side, contracts, avg_price, cost, fee, opened_ts
                           FROM trades WHERE synthetic = 0 AND opened_ts > ?""", (since,)).fetchall()
    print(f"\nPaper trades: {len(trades)}")
    tot = defaultdict(float)
    for eid, venue, q, side, n, px, cost, fee, ts in trades:
        m = marks.get(eid, {})
        cells = []
        for h in HORIZONS[1:]:
            if h in m:
                ya, yb, _ = m[h]
                exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
                if exit_px is not None:
                    pnl = n * exit_px - cost - fee
                    tot[h] += pnl
                    cells.append(f"+{h}s ${pnl:+.2f}")
        print(f"  {side.upper():3} {n:g} @ {px} (${cost} + ${fee} fee) {venue}: {q[:60]}\n      " + "  ".join(cells))
    if trades:
        print("  Total mark-to-bid P&L: " + "  ".join(f"+{h}s ${v:+.2f}" for h, v in sorted(tot.items())))


def timeline(db, rows):
    """For Kalshi decisions with tape data: did the market move before we decided, and when did it react?"""
    dec = {r[0]: r for r in db.execute(
        "SELECT event_id, decided_ts, market_id, mid_at_published, mid_at_seen, mid_at_decision, action "
        "FROM decisions WHERE venue = 'kalshi'")}
    out = []
    for r in rows:
        d = dec.get(r[0])
        if not d or d[5] is None:
            continue
        eid, decided, mkt, mid_pub, mid_seen, mid_dec, action = d
        pub = r[3] or r[4]
        ref = mid_pub if mid_pub is not None else mid_seen
        ticks = db.execute("SELECT ts, yes_bid, yes_ask FROM ticks WHERE market_id = ? AND ts > ? ORDER BY ts",
                           (mkt, pub)).fetchall()
        first_move = None
        if ref is not None:
            for ts, b, a in ticks:
                if abs((b + a) / 2 - ref) >= 0.01:
                    first_move = ts
                    break
        out.append({"headline": r[2], "action": action, "pub_to_decision": decided - pub,
                    "pre_move_c": (mid_dec - ref) * 100 if ref is not None else None,
                    "first_move_after_pub": (first_move - pub) if first_move else None,
                    "beat_market": (first_move is None or decided < first_move) if ref is not None else None})
    if not out:
        return
    print(f"\nTimeline vs the market (Kalshi decisions with live tape, n={len(out)}):")
    print(row("published -> our decision", [o["pub_to_decision"] for o in out], unit="s"))
    print(row("published -> market's first 1c move", [o["first_move_after_pub"] for o in out], unit="s"))
    known = [o for o in out if o["beat_market"] is not None]
    if known:
        beat = sum(o["beat_market"] for o in known)
        print(f"  decided before the market moved: {beat}/{len(known)}")
    moved = [o for o in out if o["pre_move_c"] and abs(o["pre_move_c"]) >= 1]
    for o in moved[:5]:
        print(f"    already moved {o['pre_move_c']:+.1f}c before we decided | {o['action']:7} | {o['headline'][:70]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-minutes", type=float)
    main(ap.parse_args().since_minutes)
