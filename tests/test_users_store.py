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


async def test_token_limits_default_and_set(store):
    await store.create("dave", "h", "user")
    rec = await store.get("dave")
    # New users inherit the default (NULL) for both windows.
    assert rec.daily_token_limit is None
    assert rec.monthly_token_limit is None
    # Set an explicit daily cap + unlimited monthly (0).
    assert await store.set_token_limits("dave", daily=1000, monthly=0) is True
    rec = await store.get("dave")
    assert rec.daily_token_limit == 1000
    assert rec.monthly_token_limit == 0
    # Clear back to inherit-default (None).
    await store.set_token_limits("dave", daily=None, monthly=None)
    rec = await store.get("dave")
    assert rec.daily_token_limit is None
    assert rec.monthly_token_limit is None
    # Setting limits on a missing user returns False.
    assert await store.set_token_limits("ghost", daily=1, monthly=1) is False


async def test_init_migrates_legacy_users_table(tmp_path):
    import aiosqlite

    # Simulate a pre-existing DB without the token-limit columns.
    db_path = str(tmp_path / "legacy.db")
    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        "CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT 'user', disabled INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL)"
    )
    await conn.execute(
        "INSERT INTO users (username, password_hash, role, disabled, created_at) "
        "VALUES ('legacy', 'h', 'user', 0, 0)"
    )
    await conn.commit()
    await conn.close()

    store = SqliteUserStore(db_path)
    await store.init()  # additive migration adds the new columns
    try:
        rec = await store.get("legacy")
        assert rec is not None
        assert rec.daily_token_limit is None
        assert rec.monthly_token_limit is None
        assert await store.set_token_limits("legacy", daily=42, monthly=99) is True
        rec = await store.get("legacy")
        assert rec.daily_token_limit == 42
    finally:
        await store.close()


async def test_seed_is_idempotent(store):
    await store.seed([("alice", "seed-hash", "admin")])
    # A later password change...
    await store.set_password("alice", "changed-hash")
    # ...survives a re-seed (INSERT OR IGNORE).
    await store.seed([("alice", "seed-hash", "admin")])
    assert (await store.get("alice")).password_hash == "changed-hash"
