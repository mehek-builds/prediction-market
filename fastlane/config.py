"""Paths and environment loading shared by every entry point."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root
PKG = Path(__file__).resolve().parent


def load_env() -> None:
    """Load ROOT/.env into the environment (never overrides variables that are already set)."""
    load_dotenv(ROOT / ".env")


KALSHI_PROD_BASE = "https://api.elections.kalshi.com"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co"
_KALSHI_BASES = (KALSHI_PROD_BASE, KALSHI_DEMO_BASE)


def kalshi_base_url() -> str:
    """Host for every Kalshi call (REST and WebSocket). KALSHI_BASE_URL defaults to production; the only other value
    accepted is the demo host. A bad value raises, so a typo cannot send signed requests somewhere unexpected."""
    raw = os.environ.get("KALSHI_BASE_URL", "").strip().rstrip("/") or KALSHI_PROD_BASE
    if raw not in _KALSHI_BASES:
        raise ValueError(f"KALSHI_BASE_URL must be {KALSHI_PROD_BASE} or {KALSHI_DEMO_BASE} (got {raw!r})")
    return raw


def kalshi_api_url() -> str:
    """REST root for market data: <base>/trade-api/v2."""
    return kalshi_base_url() + "/trade-api/v2"


def kalshi_ws_url() -> str:
    """WebSocket URL on the same host as the REST base."""
    return "wss://" + kalshi_base_url().removeprefix("https://") + "/trade-api/ws/v2"


def kalshi_is_demo() -> bool:
    return kalshi_base_url() == KALSHI_DEMO_BASE


def results_dir() -> Path:
    """Runtime data folder, one per exchange so demo and production never share a ledger, backups, universe cache or
    mode/engine files: production uses fastlane/results/, the demo host uses fastlane/results-demo/. A bad
    KALSHI_BASE_URL falls back to the production folder here; the engine refuses to start on it."""
    try:
        demo = kalshi_is_demo()
    except ValueError:
        demo = False
    return PKG / ("results-demo" if demo else "results")


load_env()   # RESULTS_DIR is fixed at import, so KALSHI_BASE_URL from .env must already be in the environment
RESULTS_DIR = results_dir()
