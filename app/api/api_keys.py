"""Personal API key helpers: generate, hash, and preview.

User-generated keys are high-entropy random tokens, so a fast SHA-256 hash is
enough for storage and lets the per-request lookup in `get_identity` stay O(1)
(a slow KDF like PBKDF2 would be wrong here — it runs on every API call). The
full key is shown to the user once; afterwards only `key_prefix` is displayed.
"""
from __future__ import annotations

import hashlib
import secrets

_PREFIX = "sk-harness-"
_PREVIEW_CHARS = 14  # chars of the key kept for display (e.g. "sk-harness-AbC")


def generate_api_key() -> str:
    """Return a fresh random API key with the conventional prefix."""
    return f"{_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest used to store/look up a key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """Return a short, non-secret preview of the key for display."""
    return key[:_PREVIEW_CHARS]
