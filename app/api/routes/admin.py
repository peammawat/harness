"""Admin panel API: user management + token-usage reporting.

Every route is guarded by `require_admin` (403 for non-admins, 401 for no
credentials). Guard rails prevent an admin from locking everyone out: you
cannot delete, disable, or demote your own account or the last remaining admin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import AuthIdentity, get_usage_store, get_user_store, require_admin
from app.api.passwords import hash_password
from app.config import Settings, get_settings
from app.schemas import (
    DisabledUpdate,
    PasswordReset,
    RoleUpdate,
    UsageEventOut,
    UsageTotals,
    UserCreate,
    UserOut,
)
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore

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
