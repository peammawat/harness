"""Self-service usage: a caller's own token totals + daily trend."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_usage_store, require_auth
from app.schemas import UsageSeriesPoint, UsageTotals
from app.storage.usage_store import UsageStore

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])


@router.get("/usage/me", response_model=UsageTotals)
async def my_usage(
    identity: str = Depends(require_auth),
    usage_store: UsageStore | None = Depends(get_usage_store),
):
    if usage_store is None:
        return UsageTotals(user=identity)
    return await usage_store.user_totals(identity)


@router.get("/usage/me/series", response_model=list[UsageSeriesPoint])
async def my_usage_series(
    days: int = Query(default=30, ge=1, le=365),
    identity: str = Depends(require_auth),
    usage_store: UsageStore | None = Depends(get_usage_store),
):
    if usage_store is None:
        return []
    since = time.time() - days * 86400
    return await usage_store.series(identity, since)
