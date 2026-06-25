"""SqliteUserStore: CRUD + idempotent seeding."""
from __future__ import annotations

import pytest

from app.storage.user_store import SqliteUserStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteUserStore(str(tmp_path / "users.db"))
    await s.init()
    yield s
    await s.close()


async def test_create_get_and_duplicate(store):
    assert await store.create("alice", "hash1", "admin") is True
    rec = await store.get("alice")
    assert rec is not None
    assert rec.username == "alice"
    assert rec.role == "admin"
    assert rec.disabled is False
    # Duplicate username is rejected.
    assert await store.create("alice", "hash2", "user") is False
    assert (await store.get("alice")).password_hash == "hash1"


async def test_get_missing_returns_none(store):
    assert await store.get("ghost") is None


async def test_set_password_role_disabled(store):
    await store.create("bob", "h", "user")
    assert await store.set_password("bob", "h2") is True
    assert (await store.get("bob")).password_hash == "h2"
    assert await store.set_role("bob", "admin") is True
    assert (await store.get("bob")).role == "admin"
    assert await store.set_disabled("bob", True) is True
    assert (await store.get("bob")).disabled is True
    # Updating a missing user returns False.
    assert await store.set_role("ghost", "admin") is False


async def test_delete(store):
    await store.create("carol", "h", "user")
    assert await store.delete("carol") is True
    assert await store.get("carol") is None
    assert await store.delete("carol") is False


async def test_list_users(store):
    await store.create("a", "h", "user")
    await store.create("b", "h", "admin")
    users = await store.list_users()
    assert {u.username for u in users} == {"a", "b"}


async def test_seed_is_idempotent(store):
    await store.seed([("alice", "seed-hash", "admin")])
    # A later password change...
    await store.set_password("alice", "changed-hash")
    # ...survives a re-seed (INSERT OR IGNORE).
    await store.seed([("alice", "seed-hash", "admin")])
    assert (await store.get("alice")).password_hash == "changed-hash"
