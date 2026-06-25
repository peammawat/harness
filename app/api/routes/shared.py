"""Public read-only access to shared conversations (no authentication).

A share token unlocks a single conversation for viewing only. This router has
no `require_auth` dependency by design — access is scoped to whichever
conversation the token maps to, and only for reading.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_conversation_store
from app.schemas import ConversationDetail
from app.storage.base import ConversationStore

router = APIRouter()


@router.get("/shared/{token}", response_model=ConversationDetail)
async def get_shared_conversation(
    token: str,
    store: ConversationStore | None = Depends(get_conversation_store),
) -> ConversationDetail:
    if store is None:
        raise HTTPException(status_code=404, detail="Chat history is disabled.")
    conv = await store.get_shared_conversation(token)
    if conv is None:
        raise HTTPException(status_code=404, detail="Shared conversation not found.")
    return conv
