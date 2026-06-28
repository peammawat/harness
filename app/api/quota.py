"""Token quota resolution + enforcement.

Caps consumption per user in two rolling windows (daily + monthly), counting
input + output tokens combined. Each limit is resolved from a per-user override
(the `users` row) falling back to an admin-set default (`app_settings`, seeded
from `Settings`). A value of `0` means **unlimited**; a per-user `NULL` means
**inherit the default**.

`enforce_quota` is called before the agent runs, so an over-quota request is
rejected with 429 before any LLM/search call is made. It checks "already at or
over the cap" — a single in-flight request may overshoot slightly, which is
acceptable and standard.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status

from app.config import Settings
from app.storage.settings_store import SettingsStore
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore

DEFAULT_DAILY_TOKEN_LIMIT_KEY = "default_daily_token_limit"
DEFAULT_MONTHLY_TOKEN_LIMIT_KEY = "default_monthly_token_limit"


def start_of_today() -> float:
    """Epoch seconds at midnight today (local server time)."""
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def start_of_month() -> float:
    """Epoch seconds at the start of the current month (local server time)."""
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


async def _default_limit(
    settings_store: SettingsStore | None, settings: Settings, key: str, fallback: int
) -> int:
    if settings_store is None:
        return fallback
    raw = await settings_store.get(key)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _effective(raw: int | None, default: int) -> int | None:
    """Combine a per-user value with the default; None = unlimited."""
    value = default if raw is None else raw
    return None if value <= 0 else value


async def resolve_limits(
    user_store: UserStore | None,
    settings_store: SettingsStore | None,
    settings: Settings,
    username: str,
) -> tuple[int | None, int | None]:
    """Return (daily, monthly) effective caps; each None means unlimited."""
    record = await user_store.get(username) if user_store is not None else None
    daily_default = await _default_limit(
        settings_store, settings, DEFAULT_DAILY_TOKEN_LIMIT_KEY,
        settings.default_daily_token_limit,
    )
    monthly_default = await _default_limit(
        settings_store, settings, DEFAULT_MONTHLY_TOKEN_LIMIT_KEY,
        settings.default_monthly_token_limit,
    )
    daily = _effective(record.daily_token_limit if record else None, daily_default)
    monthly = _effective(record.monthly_token_limit if record else None, monthly_default)
    return daily, monthly


async def enforce_quota(
    usage_store: UsageStore | None,
    user_store: UserStore | None,
    settings_store: SettingsStore | None,
    settings: Settings,
    username: str,
) -> None:
    """Raise 429 if `username` has already reached a daily or monthly cap."""
    if usage_store is None:
        return  # usage not tracked → nothing to enforce against
    daily, monthly = await resolve_limits(
        user_store, settings_store, settings, username
    )
    if daily is not None:
        used = await usage_store.usage_since(username, start_of_today())
        if used >= daily:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"เกินโควต้าการใช้งานรายวัน ({used:,}/{daily:,} โทเคน) "
                    "กรุณาลองใหม่ในวันถัดไปหรือติดต่อผู้ดูแลระบบ"
                ),
            )
    if monthly is not None:
        used = await usage_store.usage_since(username, start_of_month())
        if used >= monthly:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"เกินโควต้าการใช้งานรายเดือน ({used:,}/{monthly:,} โทเคน) "
                    "กรุณาลองใหม่ในเดือนถัดไปหรือติดต่อผู้ดูแลระบบ"
                ),
            )
