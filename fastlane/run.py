"""Run the fast lane (paper only).

    python3 -m fastlane.run                     # live, forever (Ctrl-C or SIGTERM to stop)
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
from fastlane.engine import Engine, install_stop_signals  # noqa: E402


async def main(a):
    errors.install_hooks(asyncio.get_running_loop())
    print(errors.notice())
    if a.vercel:
        from fastlane import deploy
        try:
            deploy.refuse_if_demo()
        except deploy.DeployError as exc:
            sys.exit(f"error: {exc}")
    stop = asyncio.Event()
    install_stop_signals(asyncio.get_running_loop(), stop)
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
        while not stop.is_set() and (deadline is None or time.time() < deadline):
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60, max(1, (deadline or time.time() + 60) - time.time())))
            except asyncio.TimeoutError:
                print(eng.status())
        if stop.is_set():
            print("stopping (signal)")
    finally:
        # LiveTrader.stop writes heartbeat_ts 0: an immediate restart does not wait 60 s for the old engine to look dead
        await eng.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0,
                    help="run this many minutes then stop (default: forever, until Ctrl-C or SIGTERM)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--inject", action="append", help="synthetic headline (repeatable); disables live feeds")
    ap.add_argument("--vercel", type=float, nargs="?", const=30, metavar="MINUTES",
                    help="keep the Vercel dashboard in sync every MINUTES (default 30); run fastlane.deploy once first")
    args = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        sys.exit("OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in.")
    from fastlane import releases
    from fastlane.decision import entry_styles, starter_settings
    from fastlane.kalshi_tape import move_deny_re
    from fastlane.bluesky import bsky_settings
    from fastlane.x_feed import x_settings
    try:
        x_settings()        # XAI_* validation (None when the key is unset: feature off)
        bsky_settings()     # BSKY_* validation
        move_deny_re()      # MOVE_DENY_RE must compile
        entry_styles()      # ENTRY_STYLE / ENTRY_STYLE_* must be take or post
        starter_settings()  # STARTER_* numbers
        releases.settings() # RELEASE_* numbers
        releases.load_calendar()   # every calendar entry valid (a bad one names its index)
    except ValueError as exc:
        sys.exit(str(exc))
    errors.init("engine")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    finally:
        errors.flush()
