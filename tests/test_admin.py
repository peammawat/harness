"""Admin panel API: guard, user CRUD, role changes, last-admin protection."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import FakeProvider


class _EchoRegistry:
    """LLM registry stand-in: every provider is configured and reports back the
    requested name, so a chat's resolved provider is observable in the response."""

    def get(self, name=None):
        provider = FakeProvider()
        provider.name = name or "default"
        return provider

    def available(self):
        return ["anthropic", "openai", "local"]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "sk-test-123")
    monkeypatch.setenv("AUTH_USERS", "admin:adminpass,user:userpass")
    monkeypatch.setenv("ADMIN_USERS", "admin")
    monkeypatch.setenv("AUTH_SECRET", "admin-test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _login(client, username, password):
    res = client.post("/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_seeded_roles(client):
    assert _login(client, "admin", "adminpass")["role"] == "admin"
    assert _login(client, "user", "userpass")["role"] == "user"


def test_admin_guard(client):
    admin = _login(client, "admin", "adminpass")["token"]
    user = _login(client, "user", "userpass")["token"]

    assert client.get("/v1/admin/users").status_code == 401  # no creds
    assert client.get("/v1/admin/users", headers=_auth(user)).status_code == 403
    assert client.get("/v1/admin/users", headers=_auth(admin)).status_code == 200


def test_user_crud_and_login(client):
    admin = _login(client, "admin", "adminpass")["token"]

    # Create
    res = client.post(
        "/v1/admin/users",
        json={"username": "carol", "password": "carolpass", "role": "user"},
        headers=_auth(admin),
    )
    assert res.status_code == 201, res.text
    assert res.json()["username"] == "carol"
    # The new user can log in.
    assert _login(client, "carol", "carolpass")["role"] == "user"
    # Duplicate create → 409.
    assert client.post(
        "/v1/admin/users",
        json={"username": "carol", "password": "x"},
        headers=_auth(admin),
    ).status_code == 409

    # Reset password: old fails, new works.
    assert client.post(
        "/v1/admin/users/carol/password",
        json={"password": "newpass"},
        headers=_auth(admin),
    ).status_code == 204
    assert client.post(
        "/auth/login", json={"username": "carol", "password": "carolpass"}
    ).status_code == 401
    assert _login(client, "carol", "newpass")["token"]

    # Promote to admin, reflected in /auth/me.
    assert client.put(
        "/v1/admin/users/carol/role", json={"role": "admin"}, headers=_auth(admin)
    ).status_code == 204
    carol = _login(client, "carol", "newpass")["token"]
    assert client.get("/auth/me", headers=_auth(carol)).json()["role"] == "admin"

    # Delete.
    assert client.delete("/v1/admin/users/carol", headers=_auth(admin)).status_code == 204
    assert client.post(
        "/auth/login", json={"username": "carol", "password": "newpass"}
    ).status_code == 401


def test_disable_blocks_login_and_existing_token(client):
    admin = _login(client, "admin", "adminpass")["token"]
    user_token = _login(client, "user", "userpass")["token"]

    assert client.put(
        "/v1/admin/users/user/disabled",
        json={"disabled": True},
        headers=_auth(admin),
    ).status_code == 204

    # New login blocked...
    assert client.post(
        "/auth/login", json={"username": "user", "password": "userpass"}
    ).status_code == 401
    # ...and the already-issued token is rejected.
    assert client.get("/auth/me", headers=_auth(user_token)).status_code == 401


def test_delete_missing_user(client):
    admin = _login(client, "admin", "adminpass")["token"]
    assert client.delete("/v1/admin/users/ghost", headers=_auth(admin)).status_code == 404


def test_last_admin_protection(client):
    admin = _login(client, "admin", "adminpass")["token"]
    # admin is the only admin → cannot self-delete, self-disable, or self-demote.
    assert client.delete("/v1/admin/users/admin", headers=_auth(admin)).status_code == 400
    assert client.put(
        "/v1/admin/users/admin/disabled", json={"disabled": True}, headers=_auth(admin)
    ).status_code == 400
    assert client.put(
        "/v1/admin/users/admin/role", json={"role": "user"}, headers=_auth(admin)
    ).status_code == 400


def test_settings_guarded_and_toggles_registration(client):
    admin = _login(client, "admin", "adminpass")["token"]
    user = _login(client, "user", "userpass")["token"]

    # Guard: anonymous 401, non-admin 403.
    assert client.get("/v1/admin/settings").status_code == 401
    assert client.get("/v1/admin/settings", headers=_auth(user)).status_code == 403

    # Off by default, and the public capabilities flag agrees.
    settings = client.get("/v1/admin/settings", headers=_auth(admin)).json()
    assert settings["registration_enabled"] is False
    assert client.get("/v1/capabilities").json()["registration_enabled"] is False

    # Admin flips it on → persisted + reflected publicly.
    res = client.put(
        "/v1/admin/settings", json={"registration_enabled": True}, headers=_auth(admin)
    )
    assert res.status_code == 200
    assert res.json()["registration_enabled"] is True
    assert client.get("/v1/capabilities").json()["registration_enabled"] is True
    assert client.get("/v1/admin/settings", headers=_auth(admin)).json()[
        "registration_enabled"
    ] is True


def test_settings_model_provider_roundtrip(client):
    admin = _login(client, "admin", "adminpass")["token"]
    client.app.state.llm_registry = _EchoRegistry()  # accept any provider name

    # Admin picks the global model → persisted + surfaced as the public default.
    res = client.put(
        "/v1/admin/settings",
        json={"registration_enabled": False, "model_provider": "openai"},
        headers=_auth(admin),
    )
    assert res.status_code == 200
    assert res.json()["model_provider"] == "openai"
    assert client.get("/v1/admin/settings", headers=_auth(admin)).json()[
        "model_provider"
    ] == "openai"
    assert client.get("/v1/capabilities").json()["llm_providers"]["default"] == "openai"


def test_settings_rejects_unknown_model_provider(client):
    admin = _login(client, "admin", "adminpass")["token"]
    # Real registry: an unconfigured/unknown provider name is rejected.
    res = client.put(
        "/v1/admin/settings",
        json={"registration_enabled": False, "model_provider": "not-a-provider"},
        headers=_auth(admin),
    )
    assert res.status_code == 400


def test_model_provider_enforced_per_role(client):
    admin = _login(client, "admin", "adminpass")["token"]
    user = _login(client, "user", "userpass")["token"]
    client.app.state.llm_registry = _EchoRegistry()

    # Admin sets the global model to "openai".
    client.put(
        "/v1/admin/settings",
        json={"registration_enabled": False, "model_provider": "openai"},
        headers=_auth(admin),
    )

    def _chat_provider(token):
        res = client.post(
            "/v1/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "provider": "local",  # caller tries to override
                "enable_search": False,
                "stream": False,
            },
            headers=_auth(token),
        )
        assert res.status_code == 200, res.text
        return res.json()["provider"]

    # Non-admin is forced onto the global provider, ignoring req.provider.
    assert _chat_provider(user) == "openai"
    # Admin may still override per-request.
    assert _chat_provider(admin) == "local"
