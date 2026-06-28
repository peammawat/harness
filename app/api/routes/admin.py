"""Admin panel API: user management + token-usage reporting.

Every route is guarded by `require_admin` (403 for non-admins, 401 for no
credentials). Guard rails prevent an admin from locking everyone out: you
cannot delete, disable, or demote your own account or the last remaining admin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import (
    AuthIdentity,
    get_llm_registry,
    get_settings_store,
    get_usage_store,
    get_user_store,
    require_admin,
)
from app.api.passwords import hash_password
from app.api.quota import (
    DEFAULT_DAILY_TOKEN_LIMIT_KEY,
    DEFAULT_MONTHLY_TOKEN_LIMIT_KEY,
)
from app.config import Settings, get_settings
from app.llm.registry import LLMRegistry
from app.schemas import (
    AppSettings,
    DisabledUpdate,
    PasswordReset,
    RoleUpdate,
    TokenLimitsUpdate,
    UsageEventOut,
    UsageTotals,
    UserCreate,
    UserOut,
)
from app.storage.settings_store import SettingsStore
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore

REGISTRATION_KEY = "registration_enabled"
MODEL_PROVIDER_KEY = "model_provider"

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])


def _require_user_store(
    user_store: UserStore | None = Depends(get_user_store),
) -> UserStore:
    if user_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User store is not available.",
        )
    return user_store


async def _get_or_404(user_store: UserStore, username: str) -> UserOut:
    record = await user_store.get(username)
    if record is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found.")
    return UserOut(
        username=record.username,
        role=record.role,
        disabled=record.disabled,
        created_at=record.created_at,
        daily_token_limit=record.daily_token_limit,
        monthly_token_limit=record.monthly_token_limit,
    )


async def _guard_admin_access_removal(
    user_store: UserStore, identity: AuthIdentity, target: UserOut, action: str
) -> None:
    """Block removing admin access from yourself or the last admin."""
    if target.role != "admin":
        return
    if target.username == identity.username:
        raise HTTPException(status_code=400, detail=f"You cannot {action} your own account.")
    admins = [u for u in await user_store.list_users() if u.role == "admin"]
    if len(admins) <= 1:
        raise HTTPException(
            status_code=400, detail=f"Cannot {action} the last remaining admin."
        )


# --- Users ----------------------------------------------------------------

@router.get("/users", response_model=list[UserOut])
async def list_users(user_store: UserStore = Depends(_require_user_store)):
    return await user_store.list_users()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    req: UserCreate,
    user_store: UserStore = Depends(_require_user_store),
    settings: Settings = Depends(get_settings),
):
    created = await user_store.create(
        req.username,
        hash_password(req.password, iterations=settings.pbkdf2_iterations),
        req.role,
    )
    if not created:
        raise HTTPException(
            status_code=409, detail=f"User '{req.username}' already exists."
        )
    return await _get_or_404(user_store, req.username)


@router.post("/users/{username}/password", status_code=204)
async def reset_password(
    username: str,
    req: PasswordReset,
    user_store: UserStore = Depends(_require_user_store),
    settings: Settings = Depends(get_settings),
):
    await _get_or_404(user_store, username)
    await user_store.set_password(
        username, hash_password(req.password, iterations=settings.pbkdf2_iterations)
    )
    return Response(status_code=204)


@router.put("/users/{username}/role", status_code=204)
async def set_role(
    username: str,
    req: RoleUpdate,
    identity: AuthIdentity = Depends(require_admin),
    user_store: UserStore = Depends(_require_user_store),
):
    target = await _get_or_404(user_store, username)
    if req.role != "admin":
        await _guard_admin_access_removal(user_store, identity, target, "demote")
    await user_store.set_role(username, req.role)
    return Response(status_code=204)


@router.put("/users/{username}/disabled", status_code=204)
async def set_disabled(
    username: str,
    req: DisabledUpdate,
    identity: AuthIdentity = Depends(require_admin),
    user_store: UserStore = Depends(_require_user_store),
):
    target = await _get_or_404(user_store, username)
    if req.disabled:
        await _guard_admin_access_removal(user_store, identity, target, "disable")
    await user_store.set_disabled(username, req.disabled)
    return Response(status_code=204)


@router.put("/users/{username}/token-limits", status_code=204)
async def set_token_limits(
    username: str,
    req: TokenLimitsUpdate,
    user_store: UserStore = Depends(_require_user_store),
):
    await _get_or_404(user_store, username)
    await user_store.set_token_limits(
        username, daily=req.daily_token_limit, monthly=req.monthly_token_limit
    )
    return Response(status_code=204)


@router.delete("/users/{username}", status_code=204)
async def delete_user(
    username: str,
    identity: AuthIdentity = Depends(require_admin),
    user_store: UserStore = Depends(_require_user_store),
):
    target = await _get_or_404(user_store, username)
    await _guard_admin_access_removal(user_store, identity, target, "delete")
    await user_store.delete(username)
    return Response(status_code=204)


# --- App settings ---------------------------------------------------------

def _require_settings_store(
    settings_store: SettingsStore | None = Depends(get_settings_store),
) -> SettingsStore:
    if settings_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Settings store is not available.",
        )
    return settings_store


async def _get_int(
    settings_store: SettingsStore, key: str, fallback: int
) -> int:
    raw = await settings_store.get(key)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


@router.get("/settings", response_model=AppSettings)
async def get_app_settings(
    settings: Settings = Depends(get_settings),
    settings_store: SettingsStore = Depends(_require_settings_store),
):
    return AppSettings(
        registration_enabled=await settings_store.get_bool(
            REGISTRATION_KEY, settings.registration_enabled
        ),
        model_provider=(
            await settings_store.get(MODEL_PROVIDER_KEY)
            or settings.default_llm_provider
        ),
        default_daily_token_limit=await _get_int(
            settings_store, DEFAULT_DAILY_TOKEN_LIMIT_KEY,
            settings.default_daily_token_limit,
        ),
        default_monthly_token_limit=await _get_int(
            settings_store, DEFAULT_MONTHLY_TOKEN_LIMIT_KEY,
            settings.default_monthly_token_limit,
        ),
    )


@router.put("/settings", response_model=AppSettings)
async def update_app_settings(
    req: AppSettings,
    settings: Settings = Depends(get_settings),
    settings_store: SettingsStore = Depends(_require_settings_store),
    llm: LLMRegistry = Depends(get_llm_registry),
):
    if req.registration_enabled is not None:
        await settings_store.set_bool(REGISTRATION_KEY, req.registration_enabled)
    if req.model_provider:
        try:
            llm.get(req.model_provider)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await settings_store.set(MODEL_PROVIDER_KEY, req.model_provider)
    if req.default_daily_token_limit is not None:
        await settings_store.set(
            DEFAULT_DAILY_TOKEN_LIMIT_KEY, str(req.default_daily_token_limit)
        )
    if req.default_monthly_token_limit is not None:
        await settings_store.set(
            DEFAULT_MONTHLY_TOKEN_LIMIT_KEY, str(req.default_monthly_token_limit)
        )
    return await get_app_settings(settings, settings_store)


# --- Usage ----------------------------------------------------------------

@router.get("/usage", response_model=list[UsageTotals])
async def usage_totals(usage_store: UsageStore | None = Depends(get_usage_store)):
    if usage_store is None:
        return []
    return await usage_store.totals()


@router.get("/usage/recent", response_model=list[UsageEventOut])
async def usage_recent(
    user: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    usage_store: UsageStore | None = Depends(get_usage_store),
):
    if usage_store is None:
        return []
    return await usage_store.recent(user, limit)
