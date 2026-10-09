"""Jev benchmark: latency, cost and answer stability on real headlines vs live markets.

Usage (from repo root):
    python3 -m fastlane.bench_jev --dry-run          # sources only, no Jev calls
    python3 -m fastlane.bench_jev --n 200 --repeats 3
"""
import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone

import httpx

from fastlane.config import RESULTS_DIR, load_env
from fastlane.decision import build_request, decide
from fastlane.feeds import fetch_snapshot
from fastlane.universe import Universe


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


async def run(n: int, repeats: int, concurrency: int, dry_run: bool):
    load_env()
    async with httpx.AsyncClient(timeout=15) as client:
        print("Fetching headlines and markets...")
        headlines = await fetch_snapshot(client)
        universe = Universe()
        await universe.load(client)
        markets = universe.markets
    print(f"  {len(headlines)} headlines, {len(markets)} open markets "
          f"({sum(m['venue'] == 'kalshi' for m in markets)} Kalshi, "
          f"{sum(m['venue'] == 'polymarket' for m in markets)} Polymarket)")

    items = []
    for h in headlines:
        cands = universe.shortlist(h["headline"] + " " + h["summary"])
        if cands:
            items.append((h, cands))
    items = items[:n]
    print(f"  {len(items)} headlines have at least one keyword-matched market (using up to {n})")

    if dry_run:
        for h, cands in items[:5]:
            print(f"\n  [{h['source']}] {h['headline']}")
            for c in cands[:3]:
                print(f"     -> {c['venue']}: {c['question'][:100]}")
        print("\nDry run only. Set OPENROUTER_API_KEY in .env and rerun without --dry-run.")
        return

    from fastlane.jev_client import JevClient
    jev = JevClient()
    sem = asyncio.Semaphore(concurrency)

    # Warm-up call so the first measured latency is not a cold TLS handshake.
    state, questions, _ = build_request(*items[0])
    warm = await jev.decide(state, questions)
    if "error" in warm:
        print(f"Jev call failed: {warm}")
        await jev.aclose()
        return
    print(f"  warm-up ok in {warm['latency_ms']:.0f} ms (cold, excluded)")

    async def one(idx, rep, h, cands):
        state, questions, keyed = build_request(h, cands)
        async with sem:
            res = await jev.decide(state, questions)
        answers = res.get("answers", {})
        d = decide(answers, keyed) if answers else {"action": "ERROR"}
        return {
            "idx": idx, "rep": rep, "source": h["source"], "headline": h["headline"],
            "latency_ms": res["latency_ms"], "error": res.get("error"),
            "cost": (res.get("usage") or {}).get("cost"),
            "input_tokens": (res.get("usage") or {}).get("input_tokens"),
            "answers": answers, "strength": d.get("strength"),
            "chosen_market": keyed.get(d.get("key"), {}).get("question") if d.get("key") else None,
            "decision": d["action"],
        }

    t0 = time.perf_counter()
    rows = await asyncio.gather(*(one(i, r, h, c) for i, (h, c) in enumerate(items) for r in range(repeats)))
    wall = time.perf_counter() - t0
    await jev.aclose()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = RESULTS_DIR / f"jev-bench-{stamp}.jsonl"
    raw_path.write_text("\n".join(json.dumps(r) for r in rows))

    ok = [r for r in rows if not r["error"]]
    lat = [r["latency_ms"] for r in ok]
    cost = sum(r["cost"] or 0 for r in ok)

    # Stability: for each headline, compare repeats.
    by_idx = {}
    for r in ok:
        by_idx.setdefault(r["idx"], []).append(r)
    groups = [g for g in by_idx.values() if len(g) > 1]
    noul_spread = [max(x["strength"] or 0 for x in g) - min(x["strength"] or 0 for x in g) for g in groups]
    choice_flip = sum(len({x["chosen_market"] for x in g}) > 1 for g in groups)
    decision_flip = sum(len({x["decision"] for x in g}) > 1 for g in groups)
    decisions = [g[0]["decision"] for g in by_idx.values()]

    summary = {
        "model": jev.model, "calls": len(rows), "errors": len(rows) - len(ok),
        "headlines": len(items), "repeats": repeats, "concurrency": concurrency,
        "wall_seconds": round(wall, 1),
        "latency_ms": {"min": round(min(lat)), "p50": round(pct(lat, .5)), "p90": round(pct(lat, .9)),
                       "p99": round(pct(lat, .99)), "max": round(max(lat)),
                       "mean": round(statistics.mean(lat))} if lat else None,
        "total_cost_usd": round(cost, 6),
        "cost_per_decision_usd": round(cost / len(ok), 8) if ok else None,
        "stability": {
            "headlines_compared": len(groups),
            "signal_spread_p50": round(pct(noul_spread, .5), 4) if noul_spread else None,
            "signal_spread_max": round(max(noul_spread), 4) if noul_spread else None,
            "market_choice_flips": choice_flip,
            "trade_decision_flips": decision_flip,
        },
        "decisions": {d: decisions.count(d) for d in set(decisions)},
        "raw": str(raw_path),
    }
    (RESULTS_DIR / f"jev-bench-{stamp}-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

    trades = [g[0] for g in by_idx.values() if g[0]["decision"] != "PASS"]
    if trades:
        print("\nWould-trade examples:")
        for t in trades[:10]:
            print(f"  {t['decision']:8} {t['headline'][:80]}\n           -> {str(t['chosen_market'])[:90]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.n, a.repeats, a.concurrency, a.dry_run))


if __name__ == "__main__":
    main()
