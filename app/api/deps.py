"""Shared FastAPI dependencies: auth + access to app-scoped registries."""
from __future__ import annotations

import httpx
from fastapi import Depends, Header, HTTPException, Request, status

from app.api.auth import verify_token
from app.config import Settings, get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry
from app.storage.base import ConversationStore


def get_llm_registry(request: Request) -> LLMRegistry:
    return request.app.state.llm_registry


def get_search_registry(request: Request) -> SearchRegistry:
    return request.app.state.search_registry


def get_conversation_store(request: Request) -> ConversationStore | None:
    return request.app.state.conversation_store


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def require_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Authorize a request via a web session token OR a programmatic API key.

    Returns the identity (username for token auth, the key for API-key auth).
    """
    token = _bearer_token(authorization)
    if token:
        username = verify_token(token, settings)
        if username:
            return username

    if x_api_key and x_api_key in settings.api_key_set:
        return x_api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid credentials (log in, or send a valid X-API-Key).",
    )
