"""Per-user chat persistence: storage on chat, list/get/delete, user isolation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import FakeProvider

ALICE = "sk-alice-key"
BOB = "sk-bob-key"


class _FakeLLMRegistry:
    """Returns a FakeProvider regardless of the requested name."""

    def get(self, name=None):
        return FakeProvider()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", f"{ALICE},{BOB}")
    monkeypatch.setenv("CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "true")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        # Swap in a fake LLM so no network/API key is needed.
        app.state.llm_registry = _FakeLLMRegistry()
        yield c
    get_settings.cache_clear()


def _chat(client, key, content, conversation_id=None):
    body = {
        "messages": [{"role": "user", "content": content}],
        "enable_search": False,
        "stream": False,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    res = client.post("/v1/chat", json=body, headers={"X-API-Key": key})
    assert res.status_code == 200, res.text
    return res.json()


def test_chat_persists_conversation(client):
    data = _chat(client, ALICE, "hello there")
    cid = data["conversation_id"]
    assert cid

    listing = client.get("/v1/conversations", headers={"X-API-Key": ALICE}).json()
    assert len(listing) == 1
    assert listing[0]["id"] == cid
    assert listing[0]["title"] == "hello there"
    assert listing[0]["message_count"] == 2  # user + assistant

    detail = client.get(
        f"/v1/conversations/{cid}", headers={"X-API-Key": ALICE}
    ).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "hello there"
    assert "Final answer." in detail["messages"][1]["content"]


def test_continue_existing_conversation(client):
    cid = _chat(client, ALICE, "first")["conversation_id"]
    again = _chat(client, ALICE, "second", conversation_id=cid)
    assert again["conversation_id"] == cid

    listing = client.get("/v1/conversations", headers={"X-API-Key": ALICE}).json()
    assert len(listing) == 1  # still one conversation
    assert listing[0]["message_count"] == 4  # 2 turns * (user + assistant)


def test_user_isolation(client):
    cid = _chat(client, ALICE, "alice secret")["conversation_id"]

    # Bob cannot see Alice's conversation in his listing...
    bob_list = client.get("/v1/conversations", headers={"X-API-Key": BOB}).json()
    assert bob_list == []

    # ...nor fetch it by id.
    res = client.get(f"/v1/conversations/{cid}", headers={"X-API-Key": BOB})
    assert res.status_code == 404

    # ...nor delete it.
    res = client.delete(f"/v1/conversations/{cid}", headers={"X-API-Key": BOB})
    assert res.status_code == 404

    # Passing someone else's id starts a fresh conversation for Bob, not a hijack.
    bob_chat = _chat(client, BOB, "bob msg", conversation_id=cid)
    assert bob_chat["conversation_id"] != cid


def test_delete_own_conversation(client):
    cid = _chat(client, ALICE, "to delete")["conversation_id"]
    res = client.delete(f"/v1/conversations/{cid}", headers={"X-API-Key": ALICE})
    assert res.status_code == 204

    res = client.get(f"/v1/conversations/{cid}", headers={"X-API-Key": ALICE})
    assert res.status_code == 404
    assert client.get("/v1/conversations", headers={"X-API-Key": ALICE}).json() == []


def test_history_endpoints_require_auth(client):
    assert client.get("/v1/conversations").status_code == 401


def test_share_and_view_without_auth(client):
    cid = _chat(client, ALICE, "shared chat")["conversation_id"]

    res = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    )
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    assert token

    # The public endpoint needs no credentials and returns the messages.
    shared = client.get(f"/shared/{token}")
    assert shared.status_code == 200
    body = shared.json()
    assert body["id"] == cid
    assert body["title"] == "shared chat"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_share_page_has_open_graph_tags(client):
    cid = _chat(client, ALICE, "shared chat")["conversation_id"]
    token = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    ).json()["token"]

    # The /s/{token} page is server-rendered HTML carrying OG meta tags so link
    # previews work on platforms whose crawlers don't run JavaScript.
    page = client.get(f"/s/{token}")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    assert '<meta property="og:title" content="shared chat"' in html
    assert '<meta property="og:description" content="shared chat"' in html
    assert '<meta property="og:type" content="website"' in html
    # Real browsers are redirected to the SPA view.
    assert f"/?s={token}" in html


def test_share_page_missing_token_is_404_html(client):
    page = client.get("/s/does-not-exist")
    assert page.status_code == 404
    assert "text/html" in page.headers["content-type"]


def test_share_token_is_stable(client):
    cid = _chat(client, ALICE, "stable")["conversation_id"]
    t1 = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    ).json()["token"]
    t2 = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    ).json()["token"]
    assert t1 == t2


def test_revoke_share(client):
    cid = _chat(client, ALICE, "to revoke")["conversation_id"]
    token = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    ).json()["token"]
    assert client.get(f"/shared/{token}").status_code == 200

    res = client.delete(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": ALICE}
    )
    assert res.status_code == 204
    assert client.get(f"/shared/{token}").status_code == 404


def test_share_requires_ownership(client):
    cid = _chat(client, ALICE, "alice only")["conversation_id"]
    res = client.post(
        f"/v1/conversations/{cid}/share", headers={"X-API-Key": BOB}
    )
    assert res.status_code == 404


def test_view_unknown_share_token(client):
    assert client.get("/shared/nope-not-real").status_code == 404


@pytest.fixture
def disabled_client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", ALICE)
    monkeypatch.setenv("CHAT_HISTORY_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        app.state.llm_registry = _FakeLLMRegistry()
        yield c
    get_settings.cache_clear()


def test_disabled_history_still_chats_but_no_storage(disabled_client):
    data = _chat(disabled_client, ALICE, "hi")
    assert data["conversation_id"] is None
    res = disabled_client.get("/v1/conversations", headers={"X-API-Key": ALICE})
    assert res.status_code == 404
