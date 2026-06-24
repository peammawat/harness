"""Auth tests: token round-trip, credential check, login route, auth gate."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.api.auth import authenticate, create_token, verify_token
from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_keys="sk-test-123",
        auth_users="alice:secret,bob:hunter2",
        auth_secret="unit-test-secret",
    )


# --- Unit: tokens + credentials -------------------------------------------

def test_token_round_trips(settings):
    token, _ = create_token("alice", settings)
    assert verify_token(token, settings) == "alice"


def test_token_rejects_tampered_signature(settings):
    token, _ = create_token("alice", settings)
    payload, _, _sig = token.partition(".")
    tampered = f"{payload}.deadbeef"
    assert verify_token(tampered, settings) is None


def test_token_rejects_expired(settings):
    settings.auth_token_ttl_seconds = -1  # already expired
    token, _ = create_token("alice", settings)
    assert verify_token(token, settings) is None


def test_token_rejects_unknown_user(settings):
    token, _ = create_token("alice", settings)
    settings.auth_users = "bob:hunter2"  # alice no longer exists
    assert verify_token(token, settings) is None


def test_authenticate(settings):
    assert authenticate("alice", "secret", settings) is True
    assert authenticate("alice", "wrong", settings) is False
    assert authenticate("nobody", "secret", settings) is False


# --- Routes ----------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "sk-test-123")
    monkeypatch.setenv("AUTH_USERS", "alice:secret")
    monkeypatch.setenv("AUTH_SECRET", "route-test-secret")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_login_success(client):
    res = client.post("/auth/login", json={"username": "alice", "password": "secret"})
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "alice"
    assert body["token"]
    assert body["expires_at"] > int(time.time())


def test_login_bad_password(client):
    res = client.post("/auth/login", json={"username": "alice", "password": "nope"})
    assert res.status_code == 401


def test_me_with_token(client):
    token = client.post(
        "/auth/login", json={"username": "alice", "password": "secret"}
    ).json()["token"]
    res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["username"] == "alice"


def test_protected_route_rejects_bad_bearer(client):
    # Bad token + no API key → 401 from the dependency, before any search runs.
    res = client.post(
        "/v1/search",
        json={"query": "hello"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert res.status_code == 401
