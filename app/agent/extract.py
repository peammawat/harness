"""Extract readable text from attached documents (PDF / docx / txt / md).

Pure functions with no I/O: callers pass the raw bytes (already base64-decoded)
and get back plain text. A failure to parse never raises — it returns a short
placeholder so a bad attachment can't 500 the chat. Extracted text is truncated
to a caller-supplied character budget.
"""
from __future__ import annotations

from io import BytesIO

_TRUNCATED = "\n\n[…ตัดทอน…]"


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + _TRUNCATED
    return text


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def _extract_docx(raw: bytes) -> str:
    from docx import Document

    doc = Document(BytesIO(raw))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def extract_document_text(
    filename: str, media_type: str, raw: bytes, *, max_chars: int = 50_000
) -> str:
    """Extract text from a document, dispatching on media type then extension.

    Returns the extracted text truncated to ``max_chars``, or a Thai placeholder
    when the file can't be read. Never raises.
    """
    name = (filename or "").lower()
    mtype = (media_type or "").lower()

    try:
        if mtype == "application/pdf" or name.endswith(".pdf"):
            text = _extract_pdf(raw)
        elif (
            "wordprocessingml" in mtype
            or mtype == "application/msword"
            or name.endswith(".docx")
        ):
            text = _extract_docx(raw)
        else:
            # text/plain, text/markdown, and anything else we treat as UTF-8 text.
            text = _extract_text(raw)
    except Exception:  # noqa: BLE001 — a bad attachment must not break the chat
        return f"[ไม่สามารถอ่านไฟล์ {filename}]"

    text = _truncate(text, max_chars)
    return text or f"[ไฟล์ {filename} ไม่มีข้อความที่อ่านได้]"
