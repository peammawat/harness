"""Direct search endpoint (does not involve the LLM)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_search_registry, require_auth
from app.config import Settings, get_settings
from app.schemas import SearchRequest, SearchResponse
from app.search.base import SearchError
from app.search.registry import SearchRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])


@router.post("/search", response_model=SearchResponse)
async def search(
    req: SearchRequest,
    registry: SearchRegistry = Depends(get_search_registry),
    settings: Settings = Depends(get_settings),
) -> SearchResponse:
    try:
        if req.aggregate:
            results = await registry.aggregate(
                req.query, backends=req.backends, num=req.num_results
            )
            backend = "aggregate"
        else:
            backend = req.backend or settings.default_search_backend
            results = await registry.search(req.query, backend=backend, num=req.num_results)
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # Don't leak backend internals (keys, hosts, stack) to the caller.
        logger.exception("search backend error")
        raise HTTPException(status_code=502, detail="search backend error") from exc

    return SearchResponse(query=req.query, backend=backend, results=results)
