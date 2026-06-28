"""FastAPI application entrypoint."""
from __future__ import annotations

import contextlib
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    settings = get_settings()

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        # Reject oversized request bodies up front (inline base64 attachments
        # can be large); base64 inflates raw bytes by ~33%.
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_request_bytes:
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
