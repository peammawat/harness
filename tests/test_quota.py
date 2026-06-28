"""Token quota: per-user/default daily + monthly caps enforced before the LLM runs."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import FakeProvider


class _FakeLLMRegistry:
    def get(self, name=None):
        return FakeProvider()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "sk-ignore")
    monkeypatch.setenv("AUTH_USERS", "admin:adminpass,alice:alicepass")
    monkeypatch.setenv("ADMIN_USERS", "admin")
    monkeypatch.setenv("AUTH_SECRET", "quota-test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    # Generous defaults so quota is opt-in per test via the admin settings API.
    monkeypatch.setenv("DEFAULT_DAILY_TOKEN_LIMIT", "0")
    monkeypatch.setenv("DEFAULT_MONTHLY_TOKEN_LIMIT", "0")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        app.state.llm_registry = _FakeLLMRegistry()
        yield c
    get_settings.cache_clear()


def _token(client, username, password):
    res = client.post("/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _chat(client, token):
    # Each chat records 10 input + 20 output = 30 tokens (FakeProvider, no search).
    return client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "enable_search": False,
            "stream": False,
        },
        headers=_auth(token),
    )


def _set_settings(client, admin, **fields):
    res = client.put("/v1/admin/settings", json=fields, headers=_auth(admin))
    assert res.status_code == 200, res.text
    return res.json()


def test_default_daily_cap_blocks_when_exceeded(client):
    admin = _token(client, "admin", "adminpass")
    _set_settings(client, admin, default_daily_token_limit=20, default_monthly_token_limit=0)

    alice = _token(client, "alice", "alicepass")
    # First chat passes (0 used), records 30 tokens.
    assert _chat(client, alice).status_code == 200
    # Now 30 >= 20 → next chat is rejected before the LLM runs.
    blocked = _chat(client, alice)
    assert blocked.status_code == 429
    assert "รายวัน" in blocked.json()["detail"]


def test_default_monthly_cap_blocks_when_exceeded(client):
    admin = _token(client, "admin", "adminpass")
    _set_settings(client, admin, default_daily_token_limit=0, default_monthly_token_limit=20)

    alice = _token(client, "alice", "alicepass")
    assert _chat(client, alice).status_code == 200
    blocked = _chat(client, alice)
    assert blocked.status_code == 429
    assert "รายเดือน" in blocked.json()["detail"]


def test_unlimited_default_never_blocks(client):
    admin = _token(client, "admin", "adminpass")
    _set_settings(client, admin, default_daily_token_limit=0, default_monthly_token_limit=0)
    alice = _token(client, "alice", "alicepass")
    for _ in range(3):
        assert _chat(client, alice).status_code == 200


def test_per_user_override_beats_default(client):
    admin = _token(client, "admin", "adminpass")
    # Tight default that would block after one chat...
    _set_settings(client, admin, default_daily_token_limit=20, default_monthly_token_limit=0)
    # ...but Alice is set to unlimited (0) via her per-user override.
    res = client.put(
        "/v1/admin/users/alice/token-limits",
        json={"daily_token_limit": 0, "monthly_token_limit": 0},
        headers=_auth(admin),
    )
    assert res.status_code == 204

    alice = _token(client, "alice", "alicepass")
    for _ in range(3):
        assert _chat(client, alice).status_code == 200


def test_per_user_limit_persists_and_surfaces(client):
    admin = _token(client, "admin", "adminpass")
    res = client.put(
        "/v1/admin/users/alice/token-limits",
        json={"daily_token_limit": 5000, "monthly_token_limit": None},
        headers=_auth(admin),
    )
    assert res.status_code == 204

    users = client.get("/v1/admin/users", headers=_auth(admin)).json()
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["daily_token_limit"] == 5000
    assert alice["monthly_token_limit"] is None  # inherit default


def test_token_limits_unknown_user_404(client):
    admin = _token(client, "admin", "adminpass")
    res = client.put(
        "/v1/admin/users/ghost/token-limits",
        json={"daily_token_limit": 100, "monthly_token_limit": 100},
        headers=_auth(admin),
    )
    assert res.status_code == 404


def test_token_limits_guarded(client):
    user = _token(client, "alice", "alicepass")
    # Non-admin is forbidden from setting limits.
    res = client.put(
        "/v1/admin/users/alice/token-limits",
        json={"daily_token_limit": 0, "monthly_token_limit": 0},
        headers=_auth(user),
    )
    assert res.status_code == 403


def test_default_limits_roundtrip(client):
    admin = _token(client, "admin", "adminpass")
    data = _set_settings(
        client, admin, default_daily_token_limit=12345, default_monthly_token_limit=67890
    )
    assert data["default_daily_token_limit"] == 12345
    assert data["default_monthly_token_limit"] == 67890
    fetched = client.get("/v1/admin/settings", headers=_auth(admin)).json()
    assert fetched["default_daily_token_limit"] == 12345
    assert fetched["default_monthly_token_limit"] == 67890
