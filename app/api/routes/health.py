"""Health + capability discovery (no auth required)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_llm_registry, get_search_registry
from app.config import Settings, get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/v1/capabilities")
async def capabilities(
    llm: LLMRegistry = Depends(get_llm_registry),
    search: SearchRegistry = Depends(get_search_registry),
    settings: Settings = Depends(get_settings),
) -> dict:
    """List which providers/backends are actually configured."""
    return {
        "llm_providers": {
            "available": llm.available(),
            "all": ["anthropic", "openai", "local"],
            "default": settings.default_llm_provider,
        },
        "search_backends": {
            "available": search.available(),
            "all": search.all_names(),
            "default": settings.default_search_backend,
        },
    }
