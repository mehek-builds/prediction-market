import base64
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from fastlane import books
from fastlane.ledger import Ledger
from fastlane.universe import Universe

_ENV_VARS = ["OPENROUTER_API_KEY", "JEV_MODEL", "SEC_USER_AGENT", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Host env must never leak into tests."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="session")
def ed25519_pem() -> str:
    return _pem(ed25519.Ed25519PrivateKey.generate())


def pem_body(pem: str) -> str:
    return "".join(l for l in pem.splitlines() if not l.startswith("-----"))


@pytest.fixture
def tiny_universe() -> Universe:
    """A handful of real-looking markets padded with unique-token fillers so IDF of a rare word exceeds min_score 6."""
    mk = lambda venue, id, q, cat, ask, bid, vol, **kw: dict(  # noqa: E731
        venue=venue, id=id, question=q, category=cat, yes_ask=ask, yes_bid=bid, volume_24h=vol, **kw)
    markets = [
        mk("kalshi", "CPI-A", "CPI inflation report: strike 3.0", "Economics", .70, .68, 100),
        mk("kalshi", "CPI-B", "CPI inflation report: strike 3.5", "Economics", .50, .48, 900),
        mk("kalshi", "CPI-C", "CPI inflation report: strike 4.0", "Economics", .20, .18, 500),
        mk("kalshi", "AVNT-1", "Avient quarterly earnings beat", "Companies", .55, .53, 300),
        mk("polymarket", "9001", "Bitcoin ladder moonshot", "Crypto", .30, .28, 50,
           yes_token="tokY", no_token="tokN"),
        mk("kalshi", "BTC-1", "Bitcoin closes higher", "Crypto", .45, .43, 70),
        mk("kalshi", "GEN-1", "Senate passes budget bill", "Politics", .40, .38, 10),
        mk("kalshi", "GEN-2", "Rainfall exceeds average", "Weather", .60, .58, 10),
    ]
    markets += [mk("kalshi", f"F-{i}", f"filler topic fill{i}x", "Other", .5, .48, 1) for i in range(500)]
    u = Universe()
    u._set(markets)
    return u


@pytest.fixture
def book_factory():
    def make(venue="kalshi", yes_asks=(), no_asks=()):
        return books.Book(venue, "M", list(yes_asks), list(no_asks))
    return make


@pytest.fixture
def tmp_ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")
