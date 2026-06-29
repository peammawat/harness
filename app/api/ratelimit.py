"""In-process, per-IP rate limiting for brute-force-prone endpoints.

A small fixed-window limiter keyed by client IP. Single-node only (state lives in
process memory) — matches the single `pm2`-managed deployment, so no Redis needed.
Used as a FastAPI dependency: ``Depends(rate_limit(max_attempts, window_seconds))``.
"""
from __future__ import annotations

import threading
import time

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings


class _FixedWindowLimiter:
    """Counts hits per key within a rolling set of fixed windows.

    Buckets are evicted lazily on access and capped so the map can't grow
    unbounded under a flood of distinct keys (spoofed IPs).
    """

    _MAX_KEYS = 10_000

    def __init__(self) -> None:
        self._hits: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, limit: int, window: int) -> bool:
        """Record one hit for `key`; return True if it is within `limit`."""
        if limit <= 0:
            return True
        now = int(time.time())
        bucket = now // window
        with self._lock:
            if len(self._hits) > self._MAX_KEYS:
                self._evict(bucket)
            count = self._hits.get((key, bucket), 0) + 1
            self._hits[(key, bucket)] = count
            return count <= limit

    def _evict(self, current_bucket: int) -> None:
        # Drop every bucket older than the current one (called under lock).
        for k in [k for k in self._hits if k[1] < current_bucket]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Module-level singleton shared across all dependency instances.
_limiter = _FixedWindowLimiter()


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # First hop is the original client.
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(setting_attr: str, window_seconds: int = 60):
    """Build a dependency that throttles a route per client IP.

    `setting_attr` names the `Settings` field holding the per-minute limit (e.g.
    ``"rate_limit_login_per_min"``); it's read at request time so config / env
    overrides take effect without re-importing. `0` disables the limit. Each
    distinct `setting_attr` gets its own counter so endpoints don't share a budget.
    """

    async def _dep(
        request: Request, settings: Settings = Depends(get_settings)
    ) -> None:
        limit = getattr(settings, setting_attr)
        key = f"{_client_ip(request, settings)}:{setting_attr}"
        if not _limiter.hit(key, limit=limit, window=window_seconds):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )

    return _dep
