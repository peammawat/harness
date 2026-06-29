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


# --- personal API keys ----------------------------------------------------

async def test_api_key_create_list_and_resolve(store):
    await store.create("alice", "h", "user")
    kid = await store.create_api_key("alice", "hash-aaa", "sk-harness-AbC", "laptop")
    assert isinstance(kid, str) and kid

    keys = await store.list_api_keys("alice")
    assert len(keys) == 1
    assert keys[0].id == kid
    assert keys[0].name == "laptop"
    assert keys[0].key_prefix == "sk-harness-AbC"
    assert keys[0].last_used_at is None

    # Resolving a known hash returns the owner and stamps last_used_at.
    assert await store.resolve_api_key("hash-aaa") == "alice"
    assert (await store.list_api_keys("alice"))[0].last_used_at is not None
    # Unknown hash → None.
    assert await store.resolve_api_key("nope") is None


async def test_api_key_rejected_when_owner_disabled(store):
    await store.create("bob", "h", "user")
    await store.create_api_key("bob", "hash-bbb", "sk-harness-bbb", "")
    assert await store.resolve_api_key("hash-bbb") == "bob"
    await store.set_disabled("bob", True)
    assert await store.resolve_api_key("hash-bbb") is None


async def test_api_key_delete_is_owner_scoped(store):
    await store.create("alice", "h", "user")
    await store.create("bob", "h", "user")
    kid = await store.create_api_key("alice", "hash-ccc", "sk-harness-ccc", "")
    # Bob can't delete Alice's key.
    assert await store.delete_api_key("bob", kid) is False
    assert await store.resolve_api_key("hash-ccc") == "alice"
    # Owner can.
    assert await store.delete_api_key("alice", kid) is True
    assert await store.resolve_api_key("hash-ccc") is None
    assert await store.delete_api_key("alice", kid) is False


async def test_api_keys_cascade_on_rename_and_delete(store):
    await store.create("alice", "h", "user")
    await store.create_api_key("alice", "hash-ddd", "sk-harness-ddd", "k")
    # Rename re-keys the key to the new owner.
    assert await store.rename("alice", "alice2") is True
    assert await store.resolve_api_key("hash-ddd") == "alice2"
    assert len(await store.list_api_keys("alice2")) == 1
    # Deleting the user removes their keys.
    assert await store.delete("alice2") is True
    assert await store.resolve_api_key("hash-ddd") is None


async def test_seed_is_idempotent(store):
    await store.seed([("alice", "seed-hash", "admin")])
    # A later password change...
    await store.set_password("alice", "changed-hash")
    # ...survives a re-seed (INSERT OR IGNORE).
    await store.seed([("alice", "seed-hash", "admin")])
    assert (await store.get("alice")).password_hash == "changed-hash"
