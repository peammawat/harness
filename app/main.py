"""FastAPI application entrypoint."""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.api.passwords import hash_password
from app.api.routes import (
    admin,
    auth,
    chat,
    conversations,
    health,
    me,
    search,
    shared,
    usage,
)
from app.config import get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry
from app.storage import (
    SqliteConversationStore,
    SqliteSettingsStore,
    SqliteUsageStore,
    SqliteUserStore,
)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class NoCacheStaticFiles(StaticFiles):
    """Static UI files with `Cache-Control: no-cache` so browsers (and the CDN
    in front) revalidate via ETag on every load instead of serving a stale
    bundle for hours after a deploy. `no-cache` still allows cheap 304s."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.auth_secret:
        logger.warning(
            "AUTH_SECRET is not set — session tokens are signed with API_KEYS as a "
            "fallback. Anyone holding a server API key could forge a session token. "
            "Set AUTH_SECRET to a long random value in production."
        )
    client = httpx.AsyncClient(timeout=settings.request_timeout)
    app.state.http_client = client
    app.state.search_registry = SearchRegistry(client, settings)
    app.state.llm_registry = LLMRegistry(settings)
    store = None
    if settings.chat_history_enabled:
        store = SqliteConversationStore(settings.chat_db_path)
        await store.init()
    app.state.conversation_store = store

    # User + usage stores are always built (DB auth and token accounting work
    # independently of chat-history persistence). Seed users from AUTH_USERS,
    # marking ADMIN_USERS as admins; seeding is idempotent (INSERT OR IGNORE).
    user_store = SqliteUserStore(settings.auth_db_path)
    await user_store.init()
    seed = [
        (
            username,
            hash_password(password, iterations=settings.pbkdf2_iterations),
            "admin" if username in settings.admin_user_set else "user",
        )
        for username, password in settings.auth_user_map.items()
    ]
    await user_store.seed(seed)
    app.state.user_store = user_store

    usage_store = SqliteUsageStore(settings.auth_db_path)
    await usage_store.init()
    app.state.usage_store = usage_store

    # Runtime app settings (e.g. self-registration toggle) live in the same DB.
    settings_store = SqliteSettingsStore(settings.auth_db_path)
    await settings_store.init()
    app.state.settings_store = settings_store
    try:
        yield
    finally:
        await client.aclose()
        if store is not None:
            await store.close()
        await user_store.close()
        await usage_store.close()
        await settings_store.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Onebix Harness",
        version="0.1.0",
        description=(
            "Multi-provider AI agent with pluggable web search. "
            "Authenticate API calls with the X-API-Key header."
        ),
        lifespan=lifespan,
    )

    settings = get_settings()

    # Restrict CORS to the configured browser origins (no wildcard). Credentials
    # stay off — the UI sends the token via the Authorization header, not cookies.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Only advertise HSTS over HTTPS so plain-HTTP local dev isn't pinned.
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains",
            )
        return response

    _BODY_METHODS = {"POST", "PUT", "PATCH"}

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        # Reject oversized request bodies up front (inline base64 attachments can
        # be large; base64 inflates raw bytes by ~33%). Enforce on the declared
        # Content-Length, and require one on bodied requests so a chunked upload
        # (Transfer-Encoding: chunked, no Content-Length) can't slip past the cap.
        if request.method in _BODY_METHODS:
            cl = request.headers.get("content-length")
            if cl is None or not cl.isdigit():
                return JSONResponse(
                    status_code=411,
                    content={"detail": "Content-Length required"},
                )
            if int(cl) > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body too large"},
                )
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(search.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(shared.router)
    app.include_router(admin.router)
    app.include_router(usage.router)
    app.include_router(me.router)

    # Serve the static chat UI at the root (added last so /v1/* and /docs win).
    if WEB_DIR.is_dir():
        app.mount("/", NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


app = create_app()
