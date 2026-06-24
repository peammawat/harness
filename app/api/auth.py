"""Web-login auth: credential check + stateless HMAC session tokens.

Tokens are self-contained (no server-side session store) so they work across
workers/restarts as long as `token_secret` is stable. Format:

    b64url(json{"u": username, "exp": epoch_seconds}) + "." + b64url(hmac_sha256)

The signature covers the payload segment. `verify_token` re-checks the
signature, the expiry, and that the user still exists in `auth_user_map`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import Settings


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def authenticate(username: str, password: str, settings: Settings) -> bool:
    """Constant-time credential check against the configured user map."""
    expected = settings.auth_user_map.get(username)
    if expected is None:
        # Still compare to keep timing roughly constant for unknown users.
        hmac.compare_digest(password, password)
        return False
    return hmac.compare_digest(password, expected)


def create_token(username: str, settings: Settings) -> tuple[str, int]:
    """Return (token, expires_at_epoch) for an authenticated user."""
    expires_at = int(time.time()) + settings.auth_token_ttl_seconds
    payload = _b64url_encode(
        json.dumps({"u": username, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
    )
    token = f"{payload}.{_sign(payload, settings.token_secret)}"
    return token, expires_at


def verify_token(token: str, settings: Settings) -> str | None:
    """Return the username if the token is valid and unexpired, else None."""
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(payload, settings.token_secret)):
        return None
    try:
        data = json.loads(_b64url_decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    username = data.get("u")
    expires_at = data.get("exp")
    if not isinstance(username, str) or not isinstance(expires_at, int):
        return None
    if expires_at < int(time.time()):
        return None
    if username not in settings.auth_user_map:
        return None
    return username
