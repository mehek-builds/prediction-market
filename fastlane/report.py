"""Timeline + P&L report from the ledger.  python3 -m fastlane.report [--since-minutes 60]"""
import argparse
import sqlite3
import time
from collections import defaultdict

from fastlane.config import load_env
from fastlane.decision import (BUCKET_EDGES, DECISIVE_MIN, SIGNAL_THRESHOLD, shadow_settings, starter_settings,
                               strength_bucket)
from fastlane.ledger import DB_PATH, columns, mark_key

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
    quote_col = "d.quote_wait_ms" if "quote_wait_ms" in columns(db, "decisions") else "NULL"   # 0.5.0 column
    rows = db.execute(f"""
        SELECT e.id, e.source, e.headline, e.published_ts, e.seen_ts, e.synthetic,
               d.decided_ts, d.n_candidates, d.shortlist_ms, d.jev_ms, d.book_ms, d.total_ms,
               d.action, d.reason, d.market_id, d.market_question, d.p_up, d.p_down, d.materiality, d.market_conf,
               {quote_col}
        FROM events e JOIN decisions d ON d.event_id = e.id
        WHERE e.seen_ts > ? AND e.synthetic = 0""", (since,)).fetchall()
    if not rows:
        print("No live decisions in range.")
        return
    print(f"\n=== FAST LANE REPORT: {len(rows)} live news events ===\n")

    print("Pipeline stages (our code, per event):")
    print(row("market match (shortlist)", [r[8] for r in rows]))
    print(row("Jev decision call", [r[9] for r in rows]))
    print(row("live quote wait after Jev", [r[20] for r in rows]))
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

    cols = columns(db, "trades")
    book_expr = ("COALESCE(book, CASE WHEN shadow = 1 THEN 'shadow' ELSE 'live' END)" if "book" in cols else
                 ("CASE WHEN shadow = 1 THEN 'shadow' ELSE 'live' END" if "shadow" in cols else "'live'"))
    all_trades = db.execute(f"""SELECT event_id, venue, market_question, side, contracts, avg_price, cost, fee, opened_ts,
                           {"shadow" if "shadow" in cols else "0 AS shadow"},
                           {"signal_strength" if "signal_strength" in cols else "NULL AS signal_strength"},
                           {book_expr} AS book
                           FROM trades WHERE synthetic = 0 AND opened_ts > ?""", (since,)).fetchall()
    trades = [t for t in all_trades if t[11] == "live"]       # the real paper book
    shadow = [t for t in all_trades if t[11] == "shadow"]     # what the looser shadow rule would have added
    starter = [t for t in all_trades if t[11] == "starter"]   # the small stricter-than-shadow book
    print(f"\nPaper trades: {len(trades)}")
    tot = defaultdict(float)
    for eid, venue, q, side, n, px, cost, fee, ts, _sh, _sig, _book in trades:
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
    shadow_section(trades, shadow, marks, {r[0]: r[13] for r in rows})
    starter_section(starter, marks)
    orders_section(db, since)
    releases_section(db, marks, since)


def _per_contract(t, m):
    """{horizon: P&L per contract after spread (exit at the held-side bid) and fees} for one trade."""
    _eid, _venue, _q, side, n, px, _cost, fee, _ts, _sh, _sig = t[:11]
    out = {}
    for h in HORIZONS[1:]:
        if h in m:
            ya, yb, _ = m[h]
            exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
            if exit_px is not None and n:
                out[h] = exit_px - px - fee / n
    return out


def shadow_section(real, shadow, marks, reasons):
    """Shadow vs real, per contract after spread and fees, with the shadow book split by signal strength."""
    enabled, sig, dec = shadow_settings()
    print(f"\nShadow vs real{'' if enabled else ' (shadow mode disabled)'} (shadow rule: signal >= {sig:.2f}, "
          f"decisive >= {dec:.2f}; real: {SIGNAL_THRESHOLD:.2f} / {DECISIVE_MIN:.2f}; both after the cost filter).")
    print("Shadow = what loosening would ADD, per contract after spread and fees.")
    if not real and not shadow:
        print("  no real or shadow trades in range")
        return
    groups = [("real", real), ("shadow all", shadow)]
    labels = [f"<{BUCKET_EDGES[0]:.2f}", *(f"{lo:.2f}-{hi:.2f}" for lo, hi in zip(BUCKET_EDGES, BUCKET_EDGES[1:])),
              f"{BUCKET_EDGES[-1]:.2f}+"]
    for label in labels:
        groups.append((f"shadow {label}", [t for t in shadow if strength_bucket(t[10]) == label]))
    unknown = [t for t in shadow if strength_bucket(t[10]) is None]
    if unknown:
        groups.append(("shadow unknown", unknown))
    stats = [(name, ts, [_per_contract(t, marks.get(mark_key(t[0], t[11] if len(t) > 11 else t[9]), {})) for t in ts])
             for name, ts in groups if ts]
    horizons = [h for h in HORIZONS[1:] if any(h in pc for _, _, pcs in stats for pc in pcs)]
    print(f"  {'book / signal':18} {'n':>3}" + "".join(f"   {'+' + str(h) + 's avg':>11}   up/down" for h in horizons))
    for name, ts, pcs in stats:
        cells = []
        for h in horizons:
            vals = [pc[h] for pc in pcs if h in pc]
            if vals:
                up, down = sum(v > 0 for v in vals), sum(v < 0 for v in vals)
                cells.append(f"   {sum(vals) / len(vals) * 100:>+10.1f}c   {up}/{down}")
            else:
                cells.append(f"   {'-':>11}   -")
        print(f"  {name:18} {len(ts):>3}" + "".join(cells))
    if shadow:
        why = defaultdict(int)
        for t in shadow:
            why[reasons.get(t[0]) or "unknown"] += 1
        print("  shadow trades by real reason: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())))


def _pnl_by_horizon(trades, marks, book) -> dict:
    """{horizon: total mark-to-bid P&L in dollars} for trades of one book."""
    tot = defaultdict(float)
    for t in trades:
        eid, _venue, _q, side, n, _px, cost, fee = t[:8]
        for h, (ya, yb, _mid) in marks.get(mark_key(eid, book), {}).items():
            if h == 0:
                continue
            exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
            if exit_px is not None:
                tot[h] += n * exit_px - cost - fee
    return dict(tot)


def starter_section(starter, marks):
    """The starter book (small fixed size, strength >= STARTER_SIGNAL_THRESHOLD, no decisive requirement)."""
    enabled, sig, dec, size = starter_settings()
    print(f"\nStarter book{'' if enabled else ' (disabled)'} (signal >= {sig:.2f}, decisive >= {dec:.2f}, ${size:g} a trade; paper only).")
    if not starter:
        print("  no starter trades in range")
        return
    invested = sum(t[6] + t[7] for t in starter)
    tot = _pnl_by_horizon(starter, marks, "starter")
    print(f"  trades {len(starter)}  invested ${invested:.2f}  mark-to-bid P&L: "
          + ("  ".join(f"+{h}s ${v:+.2f}" for h, v in sorted(tot.items())) or "no marks yet"))


def orders_section(db, since):
    """Working and expired resting paper orders (ENTRY_STYLE=post): outcomes by book, fill ratio, time to first fill, and
    how many cents the resting price saved against the take price."""
    if "status" not in columns(db, "paper_orders"):
        return
    orders = db.execute("SELECT id, book, status, limit_price, take_price, filled, requested, created_ts "
                        "FROM paper_orders WHERE created_ts > ? AND synthetic = 0", (since,)).fetchall()
    print("\nWorking and expired orders (resting paper bids):")
    if not orders:
        print("  none in range")
        return
    first = dict(db.execute("SELECT order_id, MIN(ts) FROM paper_fills GROUP BY order_id").fetchall())
    by = defaultdict(lambda: defaultdict(int))
    for _id, book, status, *_ in orders:
        by[book][status] += 1
    for book, c in sorted(by.items()):
        done = sum(v for k, v in c.items() if k != "working")
        got = c.get("filled", 0) + c.get("partial_expired", 0)
        ratio = f"{got}/{done} ({got / done:.0%})" if done else "-"
        print(f"  {book:8} " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) + f" | fill ratio {ratio}")
    waits = [first[o[0]] - o[7] for o in orders if o[0] in first]
    if waits:
        print(row("time to first fill", waits, unit="s"))
    for book in sorted(by):
        saved = [(o[4] - o[3]) * 100 for o in orders if o[1] == book and o[5] > 0 and o[4] is not None]
        if saved:
            print(f"  {book:8} spread saved on filled orders: avg {sum(saved) / len(saved):+.1f}c (limit vs take price, n={len(saved)})")


def releases_section(db, marks, since):
    """Trades placed on scheduled data releases (events from release:<SERIES>), with their P&L per horizon."""
    if "status" not in columns(db, "releases"):
        return
    rel = db.execute("""SELECT e.id, e.source, t.market_id, t.side, t.avg_price, t.contracts, t.cost, t.fee,
                               (SELECT r.value FROM releases r WHERE r.series LIKE '%' || substr(e.source, 9) || '%'
                                  AND e.id LIKE '%' || r.period LIMIT 1)
                        FROM events e JOIN trades t ON t.event_id = e.id
                        WHERE e.source LIKE 'release:%' AND e.seen_ts > ?""", (since,)).fetchall()
    print("\nRelease trades:")
    if not rel:
        print("  none in range")
        release_books_lines(db, since)
        return
    for eid, src, mkt, side, px, n, cost, fee, value in rel:
        m = marks.get(eid, {})
        cells = []
        for h in HORIZONS[1:]:
            if h in m:
                ya, yb, _ = m[h]
                exit_px = yb if side == "yes" else (1 - ya if ya is not None else None)
                if exit_px is not None:
                    cells.append(f"+{h}s ${n * exit_px - cost - fee:+.2f}")
        print(f"  {src:22} value {value if value is not None else '?'}  {side.upper()} {n:g} @ {px} {mkt}  " + "  ".join(cells))
    release_books_lines(db, since)


def release_books_lines(db, since):
    """Order-book snapshots around each release in range: one line per market (yes bid/ask at decision, +5s, +30s, +60s)."""
    if "label" not in columns(db, "release_books"):
        return
    ids = [r[0] for r in db.execute("SELECT release_id FROM release_books GROUP BY release_id HAVING MIN(ts) > ? "
                                    "ORDER BY MIN(ts)", (since,)).fetchall()]
    order = ["decision", "+5s", "+30s", "+60s"]
    for rid in ids:
        print(f"  books around {rid} (yes bid/ask):")
        rows = db.execute("SELECT market_id, label, yes_bid, yes_ask FROM release_books WHERE release_id = ? "
                          "ORDER BY ts, market_id", (rid,)).fetchall()
        by: dict[str, dict] = {}
        for mkt, label, b, a in rows:
            by.setdefault(mkt, {})[label] = (b, a)

        def cell(x):
            return "-" if x is None else f"{x:.2f}"
        for mkt, labels in by.items():
            print(f"    {mkt:12} " + "  ".join(f"{lb} {cell(labels[lb][0])}/{cell(labels[lb][1])}" for lb in order if lb in labels))


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
    load_env()  # so the shadow thresholds printed match the engine's .env
    main(ap.parse_args().since_minutes)
