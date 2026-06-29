"""Security-hardening regression tests: CORS, headers, rate limiting,
constant-time key compare, generic error messages, column whitelist."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import _matches_server_key
from app.config import get_settings
from app.main import create_app
from app.storage.user_store import SqliteUserStore


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "sk-secret-one,sk-secret-two")
    monkeypatch.setenv("AUTH_USERS", "alice:alicepass")
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_MIN", "2")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


# --- Constant-time API-key compare -----------------------------------------

def test_matches_server_key_accepts_valid_and_rejects_invalid():
    keys = {"sk-secret-one", "sk-secret-two"}
    assert _matches_server_key("sk-secret-one", keys) is True
    assert _matches_server_key("sk-secret-two", keys) is True
    assert _matches_server_key("sk-wrong", keys) is False
    assert _matches_server_key("", keys) is False
    assert _matches_server_key("sk-secret-one", set()) is False


def test_valid_api_key_authenticates(client):
    res = client.get("/auth/me", headers={"X-API-Key": "sk-secret-one"})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "user"


def test_bad_api_key_rejected(client):
    res = client.get("/auth/me", headers={"X-API-Key": "sk-nope"})
    assert res.status_code == 401, res.text


# --- Rate limiting ----------------------------------------------------------

def test_login_rate_limited(client):
    # Limit is 2/min; the 3rd attempt within the window is throttled regardless
    # of whether the credentials are valid.
    for _ in range(2):
        res = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        assert res.status_code == 401, res.text
    res = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert res.status_code == 429, res.text
    assert "Retry-After" in res.headers


# --- Security headers -------------------------------------------------------

def test_security_headers_present(client):
    res = client.get("/v1/capabilities")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert res.headers.get("Referrer-Policy") == "no-referrer"


# --- CORS allowlist ---------------------------------------------------------

def test_cors_allows_configured_origin(client):
    res = client.get(
        "/v1/capabilities", headers={"Origin": "https://app.example.com"}
    )
    assert res.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_rejects_unknown_origin(client):
    res = client.get("/v1/capabilities", headers={"Origin": "https://evil.example.com"})
    # Starlette omits the ACAO header for disallowed origins.
    assert res.headers.get("access-control-allow-origin") != "https://evil.example.com"


# --- Generic error messages (no internals leaked) ---------------------------

def test_search_error_is_generic(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret backend detail with api-key=xyz")

    monkeypatch.setattr("app.search.registry.SearchRegistry.search", boom)
    res = client.post(
        "/v1/search",
        json={"query": "hello"},
        headers={"X-API-Key": "sk-secret-one"},
    )
    assert res.status_code == 502, res.text
    assert res.json()["detail"] == "search backend error"
    assert "secret backend detail" not in res.text


# --- Column whitelist in user_store._update ---------------------------------

@pytest.mark.asyncio
async def test_update_rejects_non_whitelisted_column(tmp_path):
    store = SqliteUserStore(str(tmp_path / "users.db"))
    await store.init()
    try:
        with pytest.raises(ValueError):
            await store._update("disabled; DROP TABLE users", 1, "alice")
        # A whitelisted column still works.
        assert await store._update("role", "user", "nobody") is False
    finally:
        await store.close()
