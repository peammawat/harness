"""Login endpoint + token validation for the web UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import authenticate, create_token
from app.api.deps import AuthIdentity, get_identity, get_user_store
from app.config import Settings, get_settings
from app.schemas import LoginRequest, LoginResponse
from app.storage.user_store import UserStore

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


@router.get("/me")
async def me(identity: AuthIdentity = Depends(get_identity)) -> dict:
    """Validate the caller's token / key; returns the resolved identity + role."""
    return {"username": identity.username, "role": identity.role}
