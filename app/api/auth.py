"""Web-login auth: credential check + stateless HMAC session tokens.

Tokens are self-contained (no server-side session store) so they work across
workers/restarts as long as `token_secret` is stable. Format:

    b64url(json{"u": username, "exp": epoch_seconds}) + "." + b64url(hmac_sha256)

The signature covers the payload segment. `verify_token` re-checks the
signature and expiry (sync, store-free). `resolve_token` adds the "user still
exists and is not disabled" check against the DB user store (falling back to
`auth_user_map` for config-only users).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

from app.api.passwords import verify_password
from app.config import Settings

if TYPE_CHECKING:
    from app.storage.user_store import UserStore


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


async def authenticate(
    username: str,
    password: str,
    settings: Settings,
    user_store: "UserStore | None" = None,
) -> bool:
    """Verify credentials against the DB user store, falling back to config.

    A DB user takes precedence: a disabled account always fails; otherwise the
    password is checked against the stored PBKDF2 hash. Users that exist only in
    `auth_users` (no DB row yet) are checked with a constant-time compare.
    """
    if user_store is not None:
        record = await user_store.get(username)
        if record is not None:
            if record.disabled:
                return False
            return verify_password(password, record.password_hash)

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
    """Return the username if the signature is valid and unexpired, else None.

    This checks the token itself only (signature + expiry). Whether the user
    still exists / is enabled is decided by `resolve_token`, which consults the
    user store.
    """
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
    if data.get("k") is not None:
        return None  # purpose-scoped token (e.g. pwreset) — not a session token
    if expires_at < int(time.time()):
        return None
    return username


def create_reset_token(username: str, settings: Settings) -> tuple[str, int]:
    """Return (token, expires_at_epoch) for a password-reset link.

    Same HMAC scheme as `create_token` but carries `"k": "pwreset"` and a short
    TTL, so a session token can never be accepted as a reset token (and vice
    versa) — see `verify_reset_token`.
    """
    expires_at = int(time.time()) + settings.password_reset_ttl_seconds
    payload = _b64url_encode(
        json.dumps(
            {"u": username, "exp": expires_at, "k": "pwreset"},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    token = f"{payload}.{_sign(payload, settings.token_secret)}"
    return token, expires_at


def verify_reset_token(token: str, settings: Settings) -> str | None:
    """Return the username for a valid, unexpired reset token, else None."""
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
    if data.get("k") != "pwreset":
        return None
    if not isinstance(username, str) or not isinstance(expires_at, int):
        return None
    if expires_at < int(time.time()):
        return None
    return username


async def resolve_token(
    token: str,
    settings: Settings,
    user_store: "UserStore | None" = None,
) -> str | None:
    """Validate a token and confirm the user is still active.

    Returns the username if the token is valid AND the user currently exists and
    is not disabled (checked against the DB user store, with a fallback to
    config `auth_users` for users that have no DB row).
    """
    username = verify_token(token, settings)
    if username is None:
        return None
    if user_store is not None:
        record = await user_store.get(username)
        if record is not None:
            return None if record.disabled else username
    # No DB row — accept only if the user is still in the config seed.
    return username if username in settings.auth_user_map else None
