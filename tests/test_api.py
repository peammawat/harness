"""API-level tests: auth enforcement and capability discovery."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "sk-test-123")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_search_requires_api_key(client):
    res = client.post("/v1/search", json={"query": "hello"})
    assert res.status_code == 401


def test_search_rejects_bad_key(client):
    res = client.post(
        "/v1/search", json={"query": "hello"}, headers={"X-API-Key": "wrong"}
    )
    assert res.status_code == 401


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_capabilities_lists_backends(client):
    caps = client.get("/v1/capabilities").json()
    assert "duckduckgo" in caps["search_backends"]["all"]
    assert "anthropic" in caps["llm_providers"]["all"]


@pytest.fixture
def small_body_client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "sk-test-123")
    monkeypatch.setenv("MAX_REQUEST_BYTES", "100")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_oversized_body_rejected(small_body_client):
    big = {"messages": [{"role": "user", "content": "x" * 500}]}
    res = small_body_client.post(
        "/v1/chat", json=big, headers={"X-API-Key": "sk-test-123"}
    )
    assert res.status_code == 413
