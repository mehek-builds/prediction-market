"""Salted PBKDF2 password hashes for the hosted dashboard (only the hash is ever deployed)."""
import hashlib
import hmac
import os

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {"salt": salt.hex(), "iterations": iterations, "hash": digest.hex()}


def check_password(password: str, rec: dict) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(rec["salt"]), int(rec["iterations"]))
        return hmac.compare_digest(digest.hex(), rec["hash"])
    except (KeyError, TypeError, ValueError):
        return False
