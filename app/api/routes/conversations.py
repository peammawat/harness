"""Per-user chat-history endpoints (list / get / delete conversations)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_conversation_store, require_auth
from app.schemas import ConversationDetail, ConversationSummary
from app.storage.base import ConversationStore

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])


def _require_store(store: ConversationStore | None) -> ConversationStore:
    if store is None:
        raise HTTPException(status_code=404, detail="Chat history is disabled.")
    return store


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    identity: str = Depends(require_auth),
    store: ConversationStore | None = Depends(get_conversation_store),
) -> list[ConversationSummary]:
    return await _require_store(store).list_conversations(identity)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    identity: str = Depends(require_auth),
    store: ConversationStore | None = Depends(get_conversation_store),
) -> ConversationDetail:
    conv = await _require_store(store).get_conversation(identity, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conv


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    identity: str = Depends(require_auth),
    store: ConversationStore | None = Depends(get_conversation_store),
) -> None:
    if not await _require_store(store).delete_conversation(identity, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
