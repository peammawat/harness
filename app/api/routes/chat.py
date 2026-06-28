"""Chat endpoint — runs the agent and streams events over SSE."""
from __future__ import annotations

import base64
import binascii
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.agent.extract import extract_document_text
from app.agent.loop import run_agent
from app.api.deps import (
    AuthIdentity,
    get_conversation_store,
    get_http_client,
    get_identity,
    get_llm_registry,
    get_search_registry,
    get_settings_store,
    get_usage_store,
    get_user_store,
    require_auth,
)
from app.api.quota import enforce_quota
from app.config import Settings, get_settings
from app.llm.base import ImagePart, Message
from app.llm.registry import LLMRegistry
from app.schemas import ChatDocument, ChatRequest, ChatResponse
from app.search.registry import SearchRegistry
from app.storage.base import ConversationStore
from app.storage.settings_store import SettingsStore
from app.storage.usage_store import UsageStore
from app.storage.user_store import UserStore

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])

MODEL_PROVIDER_KEY = "model_provider"


async def _resolve_conversation(
    req: ChatRequest, identity: str, store: ConversationStore
) -> str:
    """Continue the given conversation if owned by `identity`, else start one.

    Persists the latest user message and returns the conversation id.
    """
    last = req.messages[-1]
    conversation_id = req.conversation_id
    if not conversation_id or not await store.owns(identity, conversation_id):
        title = (last.content or "New conversation").strip()[:60] or "New conversation"
        conversation_id = await store.create_conversation(identity, title)
    await store.append_message(identity, conversation_id, last.role, last.content)
    return conversation_id


async def _record_usage(
    usage_store: UsageStore | None,
    *,
    identity: str,
    conversation_id: str | None,
    provider: str,
    model: str,
    event: dict,
) -> None:
    """Persist token usage from a `done` event; never raises (best-effort)."""
    if usage_store is None:
        return
    in_tok = int(event.get("input_tokens", 0) or 0)
    out_tok = int(event.get("output_tokens", 0) or 0)
    if not in_tok and not out_tok:
        return
    try:
        await usage_store.record(
            user=identity,
            conversation_id=conversation_id,
            provider=provider,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    except Exception:  # noqa: BLE001 — usage accounting must never break a chat
        pass


def _document_block(doc: ChatDocument, settings: Settings) -> str:
    """Decode and extract a document into a labelled, fenced text block."""
    try:
        raw = base64.b64decode(doc.data, validate=True)
    except (binascii.Error, ValueError):
        text = f"[ไม่สามารถอ่านไฟล์ {doc.filename}]"
    else:
        text = extract_document_text(
            doc.filename, doc.media_type, raw, max_chars=settings.max_document_chars
        )
    return f"[เอกสารแนบ: {doc.filename}]\n{text}\n[จบเอกสาร: {doc.filename}]"


def _to_messages(req: ChatRequest, settings: Settings) -> list[Message]:
    out: list[Message] = []
    for m in req.messages:
        content = m.content
        if m.documents:
            doc_text = "\n\n".join(_document_block(d, settings) for d in m.documents)
            content = f"{doc_text}\n\n{content}" if content else doc_text
        images = [ImagePart(media_type=i.media_type, data=i.data) for i in m.images]
        out.append(Message(role=m.role, content=content, images=images))
    return out


def _resolve(req: ChatRequest, llm: LLMRegistry, settings: Settings, provider_name: str):
    try:
        provider = llm.get(provider_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    backend = req.search_backend or settings.default_search_backend
    return provider, backend


@router.post("/chat")
async def chat(
    req: ChatRequest,
    auth: AuthIdentity = Depends(get_identity),
    llm: LLMRegistry = Depends(get_llm_registry),
    search: SearchRegistry = Depends(get_search_registry),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    store: ConversationStore | None = Depends(get_conversation_store),
    usage_store: UsageStore | None = Depends(get_usage_store),
    settings_store: SettingsStore | None = Depends(get_settings_store),
    user_store: UserStore | None = Depends(get_user_store),
    settings: Settings = Depends(get_settings),
):
    identity = auth.username
    # Reject before any LLM/search call if the caller is already over quota.
    await enforce_quota(usage_store, user_store, settings_store, settings, identity)
    # The active LLM provider is admin-controlled: non-admins are forced onto the
    # global provider; only admins may override it per-request via `req.provider`.
    global_provider = settings.default_llm_provider
    if settings_store is not None:
        global_provider = await settings_store.get(MODEL_PROVIDER_KEY) or global_provider
    provider_name = (
        (req.provider or global_provider) if auth.role == "admin" else global_provider
    )
    provider, backend = _resolve(req, llm, settings, provider_name)
    model = req.model or ""

    conversation_id = None
    if store is not None:
        conversation_id = await _resolve_conversation(req, identity, store)

    agent_events = run_agent(
        provider=provider,
        search_registry=search,
        http_client=http_client,
        settings=settings,
        messages=_to_messages(req, settings),
        search_backend=backend,
        enable_search=req.enable_search,
        deep_research=req.deep_research,
        model=req.model,
        max_tokens=settings.max_tokens,
    )

    if req.stream:
        async def event_source():
            answer = ""
            done_event: dict | None = None
            try:
                if conversation_id is not None:
                    yield {
                        "event": "conversation",
                        "data": json.dumps({"type": "conversation",
                                            "conversation_id": conversation_id}),
                    }
                async for event in agent_events:
                    if event["type"] == "done":
                        answer = event["content"]
                        done_event = event
                    yield {
                        "event": event["type"],
                        "data": json.dumps(event, ensure_ascii=False),
                    }
            except Exception as exc:  # noqa: BLE001 — surface to client as an SSE error
                yield {
                    "event": "error",
                    "data": json.dumps({"type": "error", "message": str(exc)}),
                }
            if store is not None and conversation_id is not None and answer:
                await store.append_message(
                    identity, conversation_id, "assistant", answer
                )
            if done_event is not None:
                await _record_usage(
                    usage_store,
                    identity=identity,
                    conversation_id=conversation_id,
                    provider=provider.name,
                    model=model,
                    event=done_event,
                )

        return EventSourceResponse(event_source())

    # Non-streaming: drain the loop and return a single JSON body.
    content = ""
    tool_calls = 0
    done_event: dict | None = None
    try:
        async for event in agent_events:
            if event["type"] == "done":
                content = event["content"]
                tool_calls = event["tool_calls"]
                done_event = event
            elif event["type"] == "error":
                raise HTTPException(status_code=502, detail=event["message"])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if store is not None and conversation_id is not None and content:
        await store.append_message(identity, conversation_id, "assistant", content)
    if done_event is not None:
        await _record_usage(
            usage_store,
            identity=identity,
            conversation_id=conversation_id,
            provider=provider.name,
            model=model,
            event=done_event,
        )
    return ChatResponse(
        provider=provider.name,
        model=model,
        content=content,
        tool_calls=tool_calls,
        conversation_id=conversation_id,
    )
