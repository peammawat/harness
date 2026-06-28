"""Login endpoint + token validation for the web UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import authenticate, create_token
from app.api.deps import (
    AuthIdentity,
    get_identity,
    get_settings_store,
    get_user_store,
)
from app.api.passwords import hash_password
from app.config import Settings, get_settings
from app.schemas import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
)
from app.storage.settings_store import SettingsStore
from app.storage.user_store import UserStore

REGISTRATION_KEY = "registration_enabled"

router = APIRouter(prefix="/auth")


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
) -> LoginResponse:
    if not await authenticate(req.username, req.password, settings, user_store):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )
    role = "user"
    if user_store is not None:
        record = await user_store.get(req.username)
        if record is not None:
            role = record.role
    if role != "admin" and req.username in settings.admin_user_set:
        role = "admin"
    token, expires_at = create_token(req.username, settings)
    return LoginResponse(
        token=token, username=req.username, role=role, expires_at=expires_at
    )


@router.post(
    "/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    req: RegisterRequest,
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
    settings_store: SettingsStore | None = Depends(get_settings_store),
) -> RegisterResponse:
    """Self-service signup. Gated by the runtime `registration_enabled` toggle;
    new accounts are created disabled and await admin approval before they can
    log in."""
    enabled = settings.registration_enabled
    if settings_store is not None:
        enabled = await settings_store.get_bool(REGISTRATION_KEY, enabled)
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="การสมัครสมาชิกถูกปิดอยู่",
        )
    if user_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User store is not available.",
        )
    created = await user_store.create(
        req.username,
        hash_password(req.password, iterations=settings.pbkdf2_iterations),
        "user",
        disabled=True,
    )
    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ชื่อผู้ใช้ '{req.username}' ถูกใช้งานแล้ว",
        )
    return RegisterResponse(
        message="สมัครสมาชิกสำเร็จ บัญชีของคุณรอการอนุมัติจากผู้ดูแลระบบ",
    )


@router.get("/me")
async def me(identity: AuthIdentity = Depends(get_identity)) -> dict:
    """Validate the caller's token / key; returns the resolved identity + role."""
    return {"username": identity.username, "role": identity.role}
