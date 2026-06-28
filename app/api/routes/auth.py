"""Login endpoint + token validation for the web UI."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import (
    authenticate,
    create_reset_token,
    create_token,
    verify_reset_token,
)
from app.api.deps import (
    AuthIdentity,
    get_identity,
    get_settings_store,
    get_user_store,
)
from app.api.mailer import send_email
from app.api.passwords import hash_password
from app.config import Settings, get_settings
from app.schemas import (
    ForgotPasswordRequest,
    GenericMessage,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
)
from app.storage.settings_store import SettingsStore
from app.storage.user_store import UserStore

REGISTRATION_KEY = "registration_enabled"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")

# Returned for any forgot-password request so the endpoint can't be used to
# discover which emails have accounts.
_FORGOT_MESSAGE = "หากมีบัญชีที่ผูกกับอีเมลนี้ ระบบได้ส่งลิงก์รีเซ็ตรหัสผ่านไปให้แล้ว"


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


@router.post("/forgot-password", response_model=GenericMessage)
async def forgot_password(
    req: ForgotPasswordRequest,
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
) -> GenericMessage:
    """Email a password-reset link to the account with this email, if any.

    Always returns the same generic message (and 200) regardless of whether the
    email is known, so it can't be used to enumerate accounts. A no-op when SMTP
    isn't configured."""
    if user_store is not None and settings.smtp_configured:
        record = await user_store.get_by_email(req.email.strip())
        if record is not None and not record.disabled:
            token, _ = create_reset_token(record.username, settings)
            link = f"{settings.app_base_url.rstrip('/')}/?reset={token}"
            try:
                await send_email(
                    settings,
                    req.email.strip(),
                    "รีเซ็ตรหัสผ่าน",
                    (
                        f"สวัสดี {record.username},\n\n"
                        "คลิกลิงก์ต่อไปนี้เพื่อตั้งรหัสผ่านใหม่ "
                        f"(ลิงก์จะหมดอายุใน {settings.password_reset_ttl_seconds // 60} นาที):\n\n"
                        f"{link}\n\n"
                        "หากคุณไม่ได้ร้องขอ สามารถละเว้นอีเมลนี้ได้"
                    ),
                )
            except Exception:  # mail failure must not surface to the caller
                logger.exception("failed to send password-reset email")
    return GenericMessage(message=_FORGOT_MESSAGE)


@router.post("/reset-password", response_model=GenericMessage)
async def reset_password(
    req: ResetPasswordRequest,
    settings: Settings = Depends(get_settings),
    user_store: UserStore | None = Depends(get_user_store),
) -> GenericMessage:
    """Set a new password from a valid reset token."""
    username = verify_reset_token(req.token, settings)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ลิงก์รีเซ็ตไม่ถูกต้องหรือหมดอายุแล้ว",
        )
    if user_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User store is not available.",
        )
    updated = await user_store.set_password(
        username, hash_password(req.new_password, iterations=settings.pbkdf2_iterations)
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ไม่พบบัญชีผู้ใช้",
        )
    return GenericMessage(message="ตั้งรหัสผ่านใหม่เรียบร้อยแล้ว")


@router.get("/me")
async def me(identity: AuthIdentity = Depends(get_identity)) -> dict:
    """Validate the caller's token / key; returns the resolved identity + role."""
    return {"username": identity.username, "role": identity.role}
