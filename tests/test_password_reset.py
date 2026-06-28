"""Email-based forgot/reset-password flow (mailer monkeypatched — no real SMTP)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.auth import create_reset_token
from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound emails instead of sending them."""
    box: list[tuple[str, str, str]] = []

    async def fake_send(settings, to, subject, body):
        box.append((to, subject, body))
        return True

    monkeypatch.setattr("app.api.routes.auth.send_email", fake_send)
    return box


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "sk-ignore")
    monkeypatch.setenv("AUTH_USERS", "alice:alicepass")
    monkeypatch.setenv("AUTH_SECRET", "reset-test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    # SMTP must look configured for forgot-password to attempt a send.
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, username, password):
    res = client.post("/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def _set_email(client, email):
    token = _login(client, "alice", "alicepass")
    res = client.put("/v1/me/email", json={"email": email}, headers=_auth(token))
    assert res.status_code == 204, res.text


def _token_from_email(body: str) -> str:
    return body.split("reset=", 1)[1].split()[0].strip()


def test_forgot_password_sends_link_for_known_email(client, sent):
    _set_email(client, "alice@example.com")
    res = client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    assert res.status_code == 200
    assert "message" in res.json()
    assert len(sent) == 1
    assert sent[0][0] == "alice@example.com"
    assert "reset=" in sent[0][2]


def test_forgot_password_unknown_email_is_generic_no_send(client, sent):
    res = client.post("/auth/forgot-password", json={"email": "ghost@example.com"})
    assert res.status_code == 200
    assert res.json()["message"]  # same generic message
    assert sent == []  # no email for an unknown address


def test_reset_password_with_valid_token(client, sent):
    _set_email(client, "alice@example.com")
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    token = _token_from_email(sent[0][2])

    res = client.post(
        "/auth/reset-password", json={"token": token, "new_password": "freshpass1"}
    )
    assert res.status_code == 200, res.text
    # The new password works; the old one doesn't.
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "freshpass1"}
    ).status_code == 200
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "alicepass"}
    ).status_code == 401


def test_reset_password_invalid_token_rejected(client):
    res = client.post(
        "/auth/reset-password", json={"token": "garbage.sig", "new_password": "whatever1"}
    )
    assert res.status_code == 400


def test_reset_password_expired_token_rejected(client):
    # Build a reset token that is already expired (negative TTL).
    settings = Settings(password_reset_ttl_seconds=-10, auth_secret="reset-test-secret")
    token, _ = create_reset_token("alice", settings)
    res = client.post(
        "/auth/reset-password", json={"token": token, "new_password": "whatever1"}
    )
    assert res.status_code == 400


def test_session_token_rejected_as_reset_token(client):
    session = _login(client, "alice", "alicepass")
    res = client.post(
        "/auth/reset-password", json={"token": session, "new_password": "whatever1"}
    )
    assert res.status_code == 400


def test_reset_token_rejected_as_session_token(client):
    settings = Settings(password_reset_ttl_seconds=3600, auth_secret="reset-test-secret")
    token, _ = create_reset_token("alice", settings)
    # A purpose-scoped reset token must not authenticate normal requests.
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401
