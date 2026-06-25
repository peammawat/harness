"""Token usage: done-event totals, recording, aggregation, failure tolerance."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.loop import run_agent
from app.config import Settings, get_settings
from app.llm.base import Message
from app.main import create_app
from tests.conftest import FakeProvider, FakeSearchRegistry


# --- Loop level: the done event carries token totals ----------------------

@pytest.mark.asyncio
async def test_done_event_carries_tokens():
    async with httpx.AsyncClient() as http:
        events = [
            e
            async for e in run_agent(
                provider=FakeProvider(),
                search_registry=FakeSearchRegistry(),
                http_client=http,
                settings=Settings(_env_file=None),
                messages=[Message(role="user", content="hi")],
                search_backend="duckduckgo",
                enable_search=False,  # single turn → 10 in / 20 out
            )
        ]
    done = events[-1]
    assert done["type"] == "done"
    assert done["input_tokens"] == 10
    assert done["output_tokens"] == 20


# --- API level ------------------------------------------------------------

class _FakeLLMRegistry:
    def get(self, name=None):
        return FakeProvider()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "sk-ignore")
    monkeypatch.setenv("AUTH_USERS", "admin:adminpass,alice:alicepass")
    monkeypatch.setenv("ADMIN_USERS", "admin")
    monkeypatch.setenv("AUTH_SECRET", "usage-test-secret")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
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
    res = client.post(
        "/v1/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "enable_search": False,
            "stream": False,
        },
        headers=_auth(token),
    )
    assert res.status_code == 200, res.text


def test_chat_records_usage(client):
    alice = _token(client, "alice", "alicepass")
    _chat(client, alice)

    # Alice sees her own usage.
    mine = client.get("/v1/usage/me", headers=_auth(alice)).json()
    assert mine["user"] == "alice"
    assert mine["input_tokens"] == 10
    assert mine["output_tokens"] == 20
    assert mine["events"] == 1

    # Admin sees per-user totals including Alice.
    admin = _token(client, "admin", "adminpass")
    totals = client.get("/v1/admin/usage", headers=_auth(admin)).json()
    alice_total = next(t for t in totals if t["user"] == "alice")
    assert alice_total["input_tokens"] == 10
    assert alice_total["output_tokens"] == 20

    # Recent list has the event.
    recent = client.get("/v1/admin/usage/recent", headers=_auth(admin)).json()
    assert any(e["user"] == "alice" for e in recent)


def test_usage_accumulates(client):
    alice = _token(client, "alice", "alicepass")
    _chat(client, alice)
    _chat(client, alice)
    mine = client.get("/v1/usage/me", headers=_auth(alice)).json()
    assert mine["events"] == 2
    assert mine["input_tokens"] == 20
    assert mine["output_tokens"] == 40

    series = client.get("/v1/usage/me/series", headers=_auth(alice)).json()
    assert len(series) == 1  # both events fall in the same day bucket
    assert series[0]["input_tokens"] == 20


def test_usage_write_failure_does_not_break_chat(client):
    async def _boom(**kwargs):
        raise RuntimeError("db down")

    client.app.state.usage_store.record = _boom
    alice = _token(client, "alice", "alicepass")
    # Chat still succeeds despite the usage write blowing up.
    _chat(client, alice)
