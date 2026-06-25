"""Password hashing for DB-backed users (stdlib only, no extra deps).

Hashes are stored as a single self-describing string:

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

`verify_password` is constant-time and returns False on any malformed input
rather than raising, so a corrupt row can never 500 a login.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    """Return an encoded PBKDF2-SHA256 hash for `password`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of `password` against an encoded hash."""
    try:
        algo, iter_str, salt_hex, hash_hex = encoded.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
