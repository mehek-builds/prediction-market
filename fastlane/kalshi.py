"""Kalshi request signing for the WebSocket handshake (RSA-PSS or Ed25519). Read-only: nothing here sends requests."""
import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


def load_private_key(value: str):
    """Accepts a path to a PEM file, a full PEM string, or a bare base64 DER body."""
    value = (value or "").strip()
    if not value:
        return None
    is_file = False
    if "\n" not in value and not value.startswith("-----BEGIN"):
        try:
            path = Path(value).expanduser()
            is_file = path.is_file()
        except OSError:  # a long base64 body is not a valid file name
            is_file = False
    if is_file:
        data = path.read_bytes()
    elif value.startswith("-----BEGIN"):
        data = value.encode()
    else:
        data = b"-----BEGIN PRIVATE KEY-----\n" + value.encode() + b"\n-----END PRIVATE KEY-----\n"
    try:
        return serialization.load_pem_private_key(data, password=None)
    except Exception as exc:
        raise ValueError("KALSHI_PRIVATE_KEY_PATH is not an existing file, a PEM string, or a base64 key body") from exc


class KalshiClient:
    def __init__(self, key_id: str | None = None, private_key: str | None = None):
        self.key_id = key_id if key_id is not None else os.environ.get("KALSHI_API_KEY_ID", "")
        self._key = load_private_key(private_key if private_key is not None
                                     else os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""))

    @property
    def key_type(self) -> str:
        if isinstance(self._key, rsa.RSAPrivateKey):
            return "rsa"
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            return "ed25519"
        return "none"

    @property
    def configured(self) -> bool:
        return bool(self.key_id) and self._key is not None

    def sign_headers(self, method: str, path: str) -> dict:
        if not self.configured:
            raise RuntimeError("Kalshi API key is not configured")
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?")[0]).encode()
        if self.key_type == "rsa":
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:
            sig = self._key.sign(msg)
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
