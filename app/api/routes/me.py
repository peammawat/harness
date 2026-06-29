"""Self-service account management for the logged-in user.

Lets a web-login user view their profile, change their password, set an email,
rename their account (migrating chat history + usage and re-issuing the session
token), and read their own daily/monthly token quota for the usage progress
bars. Programmatic API-key callers have no DB row, so the mutating routes 400.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.api_keys import generate_api_key, hash_api_key, key_prefix
from app.api.auth import create_token
from app.api.deps import (
    AuthIdentity,
    get_conversation_store,
    get_identity,
    get_settings_store,
    get_usage_store,
    get_user_store,
)
from app.api.passwords import hash_password, verify_password
from app.api.quota import resolve_limits, start_of_month, start_of_today
from app.config import Settings, get_settings
from app.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    EmailUpdate,
    LoginResponse,
    MeProfile,
    PasswordChange,
    QuotaStatus,
    QuotaWindow,
    UsernameChange,
)
from app.storage.base import ConversationStore
from app.storage.settings_store import SettingsStore
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore

router = APIRouter(prefix="/v1/me")

_NO_ACCOUNT = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="การจัดการบัญชีใช้ได้เฉพาะผู้ใช้ที่เข้าสู่ระบบผ่านเว็บเท่านั้น",
)


async def _require_record(user_store: UserStore | None, username: str):
    if user_store is None:
        raise _NO_ACCOUNT
    record = await user_store.get(username)
    if record is None:
        raise _NO_ACCOUNT
    return record


async def _require_web_user(
    user_store: UserStore | None, identity: AuthIdentity
):
    """Like `_require_record`, but also rejects API-key callers — so a key can
    never be used to manage (mint/revoke) keys; that needs a web login."""
    if identity.kind != "token":
        raise _NO_ACCOUNT
    return await _require_record(user_store, identity.username)


@router.get("", response_model=MeProfile)
async def my_profile(
    identity: AuthIdentity = Depends(get_identity),
    user_store: UserStore | None = Depends(get_user_store),
) -> MeProfile:
    record = await user_store.get(identity.username) if user_store else None
    if record is None:
        return MeProfile(username=identity.username, role=identity.role)
    return MeProfile(
        username=record.username,
        role=record.role,
        email=record.email,
        created_at=record.created_at,
    )


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    req: PasswordChange,
    identity: AuthIdentity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
) -> None:
    record = await _require_record(user_store, identity.username)
    if not verify_password(req.current_password, record.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="รหัสผ่านปัจจุบันไม่ถูกต้อง",
        )
    await user_store.set_password(
        identity.username,
        hash_password(req.new_password, iterations=settings.pbkdf2_iterations),
    )


@router.put("/email", status_code=status.HTTP_204_NO_CONTENT)
async def change_email(
    req: EmailUpdate,
    identity: AuthIdentity = Depends(get_identity),
    user_store: UserStore | None = Depends(get_user_store),
) -> None:
    await _require_record(user_store, identity.username)
    email = req.email.strip() or None
    if email is not None:
        existing = await user_store.get_by_email(email)
        if existing is not None and existing.username != identity.username:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="อีเมลนี้ถูกใช้กับบัญชีอื่นแล้ว",
            )
    await user_store.set_email(identity.username, email)


@router.put("/username", response_model=LoginResponse)
async def change_username(
    req: UsernameChange,
    identity: AuthIdentity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
    conversation_store: ConversationStore | None = Depends(get_conversation_store),
    usage_store: UsageStore | None = Depends(get_usage_store),
) -> LoginResponse:
    """Rename the account, migrate its data, and return a fresh session token.

    The old token embeds the old username and stops resolving the instant the
    user row is renamed, so the client must replace it with the returned one."""
    record = await _require_record(user_store, identity.username)
    if not verify_password(req.password, record.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="รหัสผ่านไม่ถูกต้อง"
        )
    new = req.new_username.strip()
    if new == identity.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ชื่อผู้ใช้ใหม่ต้องไม่ซ้ำกับชื่อเดิม",
        )
    if not await user_store.rename(identity.username, new):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ชื่อผู้ใช้ '{new}' ถูกใช้งานแล้ว",
        )
    # Migrate everything else that keys on the username.
    if conversation_store is not None:
        await conversation_store.rename_user(identity.username, new)
    if usage_store is not None:
        await usage_store.rename_user(identity.username, new)
    token, expires_at = create_token(new, settings)
    return LoginResponse(
        token=token, username=new, role=record.role, expires_at=expires_at
    )


@router.get("/quota", response_model=QuotaStatus)
async def my_quota(
    identity: AuthIdentity = Depends(get_identity),
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
    usage_store: UsageStore | None = Depends(get_usage_store),
    settings_store: SettingsStore | None = Depends(get_settings_store),
) -> QuotaStatus:
    """Daily + monthly token usage vs. the effective caps (for progress bars)."""
    daily_limit, monthly_limit = await resolve_limits(
        user_store, settings_store, settings, identity.username
    )
    daily_used = monthly_used = 0
    if usage_store is not None:
        daily_used = await usage_store.usage_since(identity.username, start_of_today())
        monthly_used = await usage_store.usage_since(
            identity.username, start_of_month()
        )
    return QuotaStatus(
        daily=QuotaWindow(used=daily_used, limit=daily_limit),
        monthly=QuotaWindow(used=monthly_used, limit=monthly_limit),
    )


# --- Personal API keys ----------------------------------------------------


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    identity: AuthIdentity = Depends(get_identity),
    user_store: UserStore | None = Depends(get_user_store),
) -> list[ApiKeyOut]:
    await _require_web_user(user_store, identity)
    keys = await user_store.list_api_keys(identity.username)
    return [
        ApiKeyOut(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    req: ApiKeyCreate,
    identity: AuthIdentity = Depends(get_identity),
    user_store: UserStore | None = Depends(get_user_store),
) -> ApiKeyCreated:
    """Generate a key and return it once — only the prefix is stored for display."""
    await _require_web_user(user_store, identity)
    name = req.name.strip()
    key = generate_api_key()
    prefix = key_prefix(key)
    key_id = await user_store.create_api_key(
        identity.username, hash_api_key(key), prefix, name
    )
    return ApiKeyCreated(
        id=key_id,
        name=name,
        key_prefix=prefix,
        created_at=time.time(),
        last_used_at=None,
        key=key,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    identity: AuthIdentity = Depends(get_identity),
    user_store: UserStore | None = Depends(get_user_store),
) -> None:
    await _require_web_user(user_store, identity)
    if not await user_store.delete_api_key(identity.username, key_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="ไม่พบ API key นี้"
        )
