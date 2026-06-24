"""Login endpoint + token validation for the web UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import authenticate, create_token
from app.api.deps import require_auth
from app.config import Settings, get_settings
from app.schemas import LoginRequest, LoginResponse

router = APIRouter(prefix="/auth")


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    if not authenticate(req.username, req.password, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )
    token, expires_at = create_token(req.username, settings)
    return LoginResponse(token=token, username=req.username, expires_at=expires_at)


@router.get("/me")
async def me(identity: str = Depends(require_auth)) -> dict:
    """Validate the caller's token / key; returns the resolved identity."""
    return {"username": identity}
