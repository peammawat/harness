"""Admin panel API: guard, user CRUD, role changes, last-admin protection."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


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
