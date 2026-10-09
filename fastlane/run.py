"""Run the fast lane (paper only).

    python3 -m fastlane.run                     # live, forever (Ctrl-C to stop)
    python3 -m fastlane.run --minutes 30        # live for 30 minutes
    python3 -m fastlane.run --inject "Fed cuts rates by 50 bps" --minutes 2   # synthetic end-to-end test
    python3 -m fastlane.run --vercel            # also keep a password-protected copy of the dashboard on Vercel

Every start is on paper. Real Kalshi orders need LIVE_TRADING_ENABLED=1 and the dashboard switch (fastlane/live.py).
"""
import argparse
import asyncio
import hashlib
import os
import sys
import time

from fastlane.config import load_env

load_env()

from fastlane import errors  # noqa: E402
from fastlane.engine import Engine  # noqa: E402


async def main(a):
    errors.install_hooks(asyncio.get_running_loop())
    print(errors.notice())
    eng = Engine(workers=a.workers)
    await eng.start(feeds=not a.inject)
    if a.vercel:
        from fastlane import deploy
        deploy.start_background(a.vercel)
    try:
        for text in a.inject or []:
            ev = {"id": "syn-" + hashlib.sha1(f"{text}{time.time()}".encode()).hexdigest()[:12],
                  "source": "synthetic", "headline": text, "summary": "", "url": "",
                  "published_ts": time.time(), "seen_ts": time.time(), "synthetic": True}
            await eng.handle(ev)
        deadline = time.time() + a.minutes * 60 if a.minutes else None
        while deadline is None or time.time() < deadline:
            await asyncio.sleep(min(60, max(1, (deadline or time.time() + 60) - time.time())))
            print(eng.status())
    finally:
        await eng.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--inject", action="append", help="synthetic headline (repeatable); disables live feeds")
    ap.add_argument("--vercel", type=float, nargs="?", const=30, metavar="MINUTES",
                    help="keep the Vercel dashboard in sync every MINUTES (default 30); run fastlane.deploy once first")
    args = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        sys.exit("OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in.")
    from fastlane.kalshi_tape import move_deny_re
    from fastlane.bluesky import bsky_settings
    from fastlane.x_feed import x_settings
    try:
        x_settings()        # XAI_* validation (None when the key is unset: feature off)
        bsky_settings()     # BSKY_* validation
        move_deny_re()      # MOVE_DENY_RE must compile
    except ValueError as exc:
        sys.exit(str(exc))
    errors.init("engine")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    finally:
        errors.flush()
