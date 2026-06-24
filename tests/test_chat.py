"""Chat request parsing: attachment schema + document injection in _to_messages."""
from __future__ import annotations

import base64

from app.api.routes.chat import _to_messages
from app.config import Settings
from app.schemas import ChatDocument, ChatImage, ChatMessage, ChatRequest


def _settings() -> Settings:
    return Settings(_env_file=None)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_request_accepts_attachments_and_deep_research():
    req = ChatRequest(
        messages=[
            ChatMessage(
                role="user",
                content="",
                images=[ChatImage(media_type="image/png", data="abc")],
            )
        ],
        deep_research=True,
    )
    # image-only message (empty content) validates
    assert req.messages[0].content == ""
    assert req.deep_research is True


def test_document_text_injected_into_content():
    req = ChatRequest(
        messages=[
            ChatMessage(
                role="user",
                content="summarize this",
                documents=[
                    ChatDocument(
                        filename="note.txt",
                        media_type="text/plain",
                        data=_b64(b"the secret content"),
                    )
                ],
            )
        ]
    )
    msgs = _to_messages(req, _settings())
    assert "the secret content" in msgs[0].content
    assert "summarize this" in msgs[0].content
    assert "[เอกสารแนบ: note.txt]" in msgs[0].content


def test_image_mapped_to_imagepart():
    req = ChatRequest(
        messages=[
            ChatMessage(
                role="user", content="hi",
                images=[ChatImage(media_type="image/jpeg", data="zzz")],
            )
        ]
    )
    msgs = _to_messages(req, _settings())
    assert len(msgs[0].images) == 1
    assert msgs[0].images[0].media_type == "image/jpeg"
    assert msgs[0].images[0].data == "zzz"


def test_bad_base64_document_yields_placeholder():
    req = ChatRequest(
        messages=[
            ChatMessage(
                role="user", content="",
                documents=[
                    ChatDocument(filename="x.txt", media_type="text/plain", data="!!!")
                ],
            )
        ]
    )
    msgs = _to_messages(req, _settings())
    assert "[ไม่สามารถอ่านไฟล์ x.txt]" in msgs[0].content
