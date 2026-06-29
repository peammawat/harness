"""Self-service account management: /v1/me profile, password, email, rename, quota."""
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
    monkeypatch.setenv("AUTH_SECRET", "me-test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
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


def _create_user(client, admin, username, password, role="user"):
    res = client.post(
        "/v1/admin/users",
        json={"username": username, "password": password, "role": role},
        headers=_auth(admin),
    )
    assert res.status_code in (200, 201), res.text


def _chat(client, token):
    return client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "enable_search": False,
            "stream": False,
        },
        headers=_auth(token),
    )


# --- password -------------------------------------------------------------

def test_change_password_wrong_current_rejected(client):
    alice = _token(client, "alice", "alicepass")
    res = client.put(
        "/v1/me/password",
        json={"current_password": "wrong", "new_password": "brandnew1"},
        headers=_auth(alice),
    )
    assert res.status_code == 400


def test_change_password_then_login_with_new(client):
    alice = _token(client, "alice", "alicepass")
    res = client.put(
        "/v1/me/password",
        json={"current_password": "alicepass", "new_password": "brandnew1"},
        headers=_auth(alice),
    )
    assert res.status_code == 204, res.text
    # Old password no longer works; new one does.
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "alicepass"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "brandnew1"}
    ).status_code == 200


# --- email ----------------------------------------------------------------

def test_set_email_and_read_profile(client):
    alice = _token(client, "alice", "alicepass")
    res = client.put(
        "/v1/me/email", json={"email": "alice@example.com"}, headers=_auth(alice)
    )
    assert res.status_code == 204, res.text
    me = client.get("/v1/me", headers=_auth(alice)).json()
    assert me["email"] == "alice@example.com"
    assert me["username"] == "alice"


def test_email_rejects_invalid_and_enforces_uniqueness(client):
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", "bobpass")
    alice = _token(client, "alice", "alicepass")
    bob = _token(client, "bob", "bobpass")

    assert client.put(
        "/v1/me/email", json={"email": "not-an-email"}, headers=_auth(alice)
    ).status_code == 422

    assert client.put(
        "/v1/me/email", json={"email": "shared@example.com"}, headers=_auth(alice)
    ).status_code == 204
    # Bob can't claim Alice's email.
    assert client.put(
        "/v1/me/email", json={"email": "shared@example.com"}, headers=_auth(bob)
    ).status_code == 409


# --- username rename ------------------------------------------------------

def test_username_rename_migrates_data_and_reissues_token(client):
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", "bobpass")
    bob = _token(client, "bob", "bobpass")

    # Produce a conversation + usage row under "bob".
    assert _chat(client, bob).status_code == 200
    convs = client.get("/v1/conversations", headers=_auth(bob)).json()
    assert len(convs) == 1

    res = client.put(
        "/v1/me/username",
        json={"new_username": "bob2", "password": "bobpass"},
        headers=_auth(bob),
    )
    assert res.status_code == 200, res.text
    new_token = res.json()["token"]
    assert res.json()["username"] == "bob2"

    # Old token stops resolving (bob has no DB row and isn't in AUTH_USERS).
    assert client.get("/auth/me", headers=_auth(bob)).status_code == 401
    # New token works and carries the migrated history + usage.
    me = client.get("/auth/me", headers=_auth(new_token)).json()
    assert me["username"] == "bob2"
    assert len(client.get("/v1/conversations", headers=_auth(new_token)).json()) == 1
    usage = client.get("/v1/usage/me", headers=_auth(new_token)).json()
    assert usage["input_tokens"] + usage["output_tokens"] == 30


def test_username_rename_to_taken_name_conflicts(client):
    bob_pass = "bobpass"
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", bob_pass)
    bob = _token(client, "bob", bob_pass)
    res = client.put(
        "/v1/me/username",
        json={"new_username": "alice", "password": bob_pass},
        headers=_auth(bob),
    )
    assert res.status_code == 409


def test_username_rename_wrong_password_rejected(client):
    alice = _token(client, "alice", "alicepass")
    res = client.put(
        "/v1/me/username",
        json={"new_username": "alice2", "password": "wrong"},
        headers=_auth(alice),
    )
    assert res.status_code == 400


# --- quota ----------------------------------------------------------------

def test_quota_endpoint_reports_used_and_limit(client):
    admin = _token(client, "admin", "adminpass")
    client.put(
        "/v1/admin/settings",
        json={"default_daily_token_limit": 1000, "default_monthly_token_limit": 5000},
        headers=_auth(admin),
    )
    alice = _token(client, "alice", "alicepass")
    q = client.get("/v1/me/quota", headers=_auth(alice)).json()
    assert q["daily"] == {"used": 0, "limit": 1000}
    assert q["monthly"] == {"used": 0, "limit": 5000}

    assert _chat(client, alice).status_code == 200
    q = client.get("/v1/me/quota", headers=_auth(alice)).json()
    assert q["daily"]["used"] == 30
    assert q["monthly"]["used"] == 30


def test_quota_unlimited_reports_null_limit(client):
    alice = _token(client, "alice", "alicepass")  # defaults are 0 = unlimited
    q = client.get("/v1/me/quota", headers=_auth(alice)).json()
    assert q["daily"]["limit"] is None
    assert q["monthly"]["limit"] is None


# --- personal API keys ----------------------------------------------------

def test_api_key_create_list_use_and_revoke(client):
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", "bobpass")
    bob = _token(client, "bob", "bobpass")

    # Create returns the full key once + only a prefix on the record.
    res = client.post("/v1/me/api-keys", json={"name": "n8n"}, headers=_auth(bob))
    assert res.status_code == 201, res.text
    created = res.json()
    full_key = created["key"]
    assert full_key.startswith("sk-harness-")
    assert created["name"] == "n8n"

    # Listing never exposes the secret, only the prefix.
    keys = client.get("/v1/me/api-keys", headers=_auth(bob)).json()
    assert len(keys) == 1
    assert "key" not in keys[0]
    assert full_key.startswith(keys[0]["key_prefix"])
    key_id = keys[0]["id"]

    # The key authenticates a /v1/* call, attributed to bob.
    res = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "enable_search": False,
            "stream": False,
        },
        headers={"X-API-Key": full_key},
    )
    assert res.status_code == 200, res.text
    usage = client.get("/v1/usage/me", headers=_auth(bob)).json()
    assert usage["input_tokens"] + usage["output_tokens"] == 30

    # Revoke → the key stops working.
    assert client.delete(
        f"/v1/me/api-keys/{key_id}", headers=_auth(bob)
    ).status_code == 204
    res = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"X-API-Key": full_key},
    )
    assert res.status_code == 401


def test_revoking_missing_key_404(client):
    alice = _token(client, "alice", "alicepass")
    assert client.delete(
        "/v1/me/api-keys/deadbeef", headers=_auth(alice)
    ).status_code == 404


def test_api_key_caller_cannot_mint_keys(client):
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", "bobpass")
    bob = _token(client, "bob", "bobpass")
    full_key = client.post(
        "/v1/me/api-keys", json={"name": ""}, headers=_auth(bob)
    ).json()["key"]
    # An API-key caller has no DB row, so minting keys is rejected.
    res = client.post("/v1/me/api-keys", json={"name": "x"}, headers={"X-API-Key": full_key})
    assert res.status_code == 400


def test_disabled_user_key_rejected(client):
    admin = _token(client, "admin", "adminpass")
    _create_user(client, admin, "bob", "bobpass")
    bob = _token(client, "bob", "bobpass")
    full_key = client.post(
        "/v1/me/api-keys", json={"name": ""}, headers=_auth(bob)
    ).json()["key"]
    # Disable bob; his key must stop authenticating immediately.
    assert client.put(
        "/v1/admin/users/bob/disabled",
        json={"disabled": True},
        headers=_auth(admin),
    ).status_code in (200, 204)
    res = client.post(
        "/v1/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers={"X-API-Key": full_key},
    )
    assert res.status_code == 401
