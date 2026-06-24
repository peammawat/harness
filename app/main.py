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

from app.api.routes import auth, chat, conversations, health, search
from app.config import get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry
from app.storage import SqliteConversationStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
    try:
        yield
    finally:
        await client.aclose()
        if store is not None:
            await store.close()


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

    # Serve the static chat UI at the root (added last so /v1/* and /docs win).
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


app = create_app()
