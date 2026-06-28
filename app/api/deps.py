"""Shared FastAPI dependencies: auth + access to app-scoped registries."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Depends, Header, HTTPException, Request, status

from app.api.auth import resolve_token
from app.config import Settings, get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry
from app.storage.base import ConversationStore
from app.storage.settings_store import SettingsStore
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore


def get_llm_registry(request: Request) -> LLMRegistry:
    return request.app.state.llm_registry


def get_search_registry(request: Request) -> SearchRegistry:
    return request.app.state.search_registry


def get_conversation_store(request: Request) -> ConversationStore | None:
    return request.app.state.conversation_store


def get_user_store(request: Request) -> UserStore | None:
    return getattr(request.app.state, "user_store", None)


def get_usage_store(request: Request) -> UsageStore | None:
    return getattr(request.app.state, "usage_store", None)


def get_settings_store(request: Request) -> SettingsStore | None:
    return getattr(request.app.state, "settings_store", None)


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@dataclass
class AuthIdentity:
    """Resolved caller identity. `username` is what storage keys on; `role`
    gates admin routes; `kind` distinguishes web tokens from API keys."""

    username: str
    role: str  # "admin" | "user"
    kind: str  # "token" | "api_key"


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _resolve_role(
    username: str, settings: Settings, user_store: UserStore | None
) -> str:
    """A user's role: the DB record's role, else admin if seeded as one."""
    if user_store is not None:
        record = await user_store.get(username)
        if record is not None:
            return record.role
    return "admin" if username in settings.admin_user_set else "user"


async def get_identity(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
) -> AuthIdentity:
    """Authorize via a web session token OR a programmatic API key.

    Returns the caller's identity (username + role + kind). API-key callers have
    no DB user row and are always treated as role "user".
    """
    token = _bearer_token(authorization)
    if token:
        username = await resolve_token(token, settings, user_store)
        if username:
            role = await _resolve_role(username, settings, user_store)
            return AuthIdentity(username=username, role=role, kind="token")

    if x_api_key and x_api_key in settings.api_key_set:
        return AuthIdentity(username=x_api_key, role="user", kind="api_key")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid credentials (log in, or send a valid X-API-Key).",
    )


async def require_auth(identity: AuthIdentity = Depends(get_identity)) -> str:
    """Authorize a request; return the identity string storage keys on.

    Preserves the original `str` contract (username for token auth, the key for
    API-key auth) so existing routes and the storage layer are unaffected.
    """
    return identity.username


async def require_admin(
    identity: AuthIdentity = Depends(get_identity),
) -> AuthIdentity:
    """Authorize a request and require the admin role (403 otherwise)."""
    if identity.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    return identity
