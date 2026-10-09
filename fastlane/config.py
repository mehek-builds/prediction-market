"""Paths and environment loading shared by every entry point."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def load_env() -> None:
    """Load ROOT/.env into the environment (never overrides variables that are already set)."""
    load_dotenv(ROOT / ".env")
