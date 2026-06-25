"""Public read-only access to shared conversations (no authentication).

A share token unlocks a single conversation for viewing only. This router has
no `require_auth` dependency by design — access is scoped to whichever
conversation the token maps to, and only for reading.
"""
from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.deps import get_conversation_store
from app.schemas import ConversationDetail
from app.storage.base import ConversationStore

router = APIRouter()

_SITE_NAME = "Onebix Harness"
_FALLBACK_TITLE = "แชทที่แชร์"
_FALLBACK_DESC = "บทสนทนาที่แชร์จาก Onebix Harness"
_DESC_LIMIT = 150


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


def _first_user_excerpt(conv: ConversationDetail) -> str:
    """Description = first user message, trimmed to a preview-friendly length."""
    for msg in conv.messages:
        if msg.role == "user" and msg.content.strip():
            text = " ".join(msg.content.split())
            if len(text) > _DESC_LIMIT:
                text = text[: _DESC_LIMIT - 1].rstrip() + "…"
            return text
    return _FALLBACK_DESC


def _share_page(token: str, title: str, description: str, url: str) -> str:
    """A minimal HTML page whose <head> carries Open Graph / Twitter Card tags.

    Social/chat crawlers don't run JavaScript, so the preview metadata must be
    present in the server response. Real browsers are redirected to the SPA
    (`/?s=<token>`) which renders the conversation read-only as before.
    """
    t = html.escape(title)
    d = html.escape(description)
    u = html.escape(url, quote=True)
    spa = "/?s=" + html.escape(token, quote=True)
    site = html.escape(_SITE_NAME)
    full_title = f"{t} · {site}"
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{full_title}</title>
  <meta name="description" content="{d}" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="{site}" />
  <meta property="og:title" content="{t}" />
  <meta property="og:description" content="{d}" />
  <meta property="og:url" content="{u}" />
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="{t}" />
  <meta name="twitter:description" content="{d}" />
  <meta http-equiv="refresh" content="0; url={spa}" />
  <script>location.replace({json.dumps(spa)});</script>
</head>
<body>
  <h1>{t}</h1>
  <p><a href="{spa}">เปิดบทสนทนาที่แชร์</a></p>
</body>
</html>"""


def _not_found_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(_FALLBACK_TITLE)} · {html.escape(_SITE_NAME)}</title>
</head>
<body>
  <h1>ลิงค์นี้ใช้ไม่ได้แล้ว</h1>
</body>
</html>"""


@router.get("/s/{token}", response_class=HTMLResponse)
async def shared_conversation_page(
    token: str,
    request: Request,
    store: ConversationStore | None = Depends(get_conversation_store),
) -> HTMLResponse:
    """Server-rendered share page with Open Graph tags for link previews."""
    conv = None if store is None else await store.get_shared_conversation(token)
    if conv is None:
        return HTMLResponse(_not_found_page(), status_code=404)
    title = conv.title or _FALLBACK_TITLE
    description = _first_user_excerpt(conv)
    return HTMLResponse(_share_page(token, title, description, str(request.url)))
