"""Health + capability discovery (no auth required)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_llm_registry, get_search_registry, get_settings_store
from app.config import Settings, get_settings
from app.llm.registry import LLMRegistry
from app.search.registry import SearchRegistry
from app.storage.settings_store import SettingsStore

router = APIRouter()

REGISTRATION_KEY = "registration_enabled"
MODEL_PROVIDER_KEY = "model_provider"


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/v1/capabilities")
async def capabilities(
    llm: LLMRegistry = Depends(get_llm_registry),
    search: SearchRegistry = Depends(get_search_registry),
    settings: Settings = Depends(get_settings),
    settings_store: SettingsStore | None = Depends(get_settings_store),
) -> dict:
    """List which providers/backends are actually configured."""
    registration_enabled = settings.registration_enabled
    default_provider = settings.default_llm_provider
    if settings_store is not None:
        registration_enabled = await settings_store.get_bool(
            REGISTRATION_KEY, registration_enabled
        )
        default_provider = (
            await settings_store.get(MODEL_PROVIDER_KEY) or default_provider
        )
    return {
        "llm_providers": {
            "available": llm.available(),
            "all": ["anthropic", "openai", "local"],
            "default": default_provider,
        },
        "search_backends": {
            "available": search.available(),
            "all": search.all_names(),
            "default": settings.default_search_backend,
        },
        "registration_enabled": registration_enabled,
    }
