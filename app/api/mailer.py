"""Outbound email via stdlib `smtplib` (no extra deps).

Used by the forgot-password flow. `smtplib` is blocking, so the actual send runs
in a worker thread via `asyncio.to_thread`. Sending is gated on
`settings.smtp_configured`; tests monkeypatch `send_email` so no real SMTP server
is ever contacted.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.config import Settings


def _send_sync(settings: Settings, to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_sender or ""
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def send_email(settings: Settings, to: str, subject: str, body: str) -> bool:
    """Send one email; return False (no raise) when SMTP isn't configured.

    Any SMTP error propagates to the caller, which logs it — a mail failure must
    not 500 the user-facing request.
    """
    if not settings.smtp_configured:
        return False
    await asyncio.to_thread(_send_sync, settings, to, subject, body)
    return True
