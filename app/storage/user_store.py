"""User store: DB-backed accounts for web login + roles.

Holds the credentials and role for each managed user. Seeded once from
`AUTH_USERS` on startup; the admin panel is the source of truth afterwards.
Mirrors `SqliteConversationStore`: one shared `aiosqlite` connection guarded by
an `asyncio.Lock` (single-node). Swap in a Redis/Postgres impl of this ABC for
scale-out — nothing else changes.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.schemas import UserOut


@dataclass
class UserRecord:
    username: str
    password_hash: str
    role: str
    disabled: bool
    created_at: float
    # Per-user token caps: None = inherit default, 0 = unlimited, >0 = cap.
    daily_token_limit: int | None = None
    monthly_token_limit: int | None = None
    email: str | None = None


@dataclass
class ApiKeyRecord:
    """A user-generated API key. The secret hash is never exposed; only the
    short `key_prefix` is shown after creation."""

    id: str
    username: str
    key_prefix: str
    name: str
    created_at: float
    last_used_at: float | None = None


class UserStore(ABC):
    @abstractmethod
    async def init(self) -> None:
        """Prepare the backing store (create tables). Idempotent."""

    @abstractmethod
    async def get(self, username: str) -> UserRecord | None:
        """Return the full record (incl. password hash), or None."""

    @abstractmethod
    async def exists(self, username: str) -> bool: ...

    @abstractmethod
    async def list_users(self) -> list[UserOut]:
        """All users, newest first (no password hashes)."""

    @abstractmethod
    async def create(
        self, username: str, password_hash: str, role: str, *, disabled: bool = False
    ) -> bool:
        """Create a user; return False if the username already exists."""

    @abstractmethod
    async def set_password(self, username: str, password_hash: str) -> bool: ...

    @abstractmethod
    async def set_role(self, username: str, role: str) -> bool: ...

    @abstractmethod
    async def set_email(self, username: str, email: str | None) -> bool: ...

    @abstractmethod
    async def get_by_email(self, email: str) -> UserRecord | None:
        """Look up a user by email (case-insensitive), or None."""

    @abstractmethod
    async def rename(self, old: str, new: str) -> bool:
        """Rename a user's primary key. False if `new` exists or `old` is missing.

        Only touches the `users` table; the caller is responsible for migrating
        other stores (conversations, usage) that key on the username.
        """

    @abstractmethod
    async def set_disabled(self, username: str, disabled: bool) -> bool: ...

    @abstractmethod
    async def set_token_limits(
        self, username: str, *, daily: int | None, monthly: int | None
    ) -> bool:
        """Set per-user token caps (None = inherit default, 0 = unlimited)."""

    @abstractmethod
    async def delete(self, username: str) -> bool: ...

    @abstractmethod
    async def seed(self, entries: list[tuple[str, str, str]]) -> None:
        """Insert (username, password_hash, role) rows, skipping existing ones."""

    # --- Personal API keys -------------------------------------------------

    @abstractmethod
    async def create_api_key(
        self, username: str, key_hash: str, key_prefix: str, name: str
    ) -> str:
        """Store a new key for `username`; return the generated key id."""

    @abstractmethod
    async def list_api_keys(self, username: str) -> list[ApiKeyRecord]:
        """A user's keys, newest first (no secrets)."""

    @abstractmethod
    async def resolve_api_key(self, key_hash: str) -> str | None:
        """Return the owner's username for an active key, else None.

        Only matches when the owning user exists and is not disabled, so
        disabling an account immediately invalidates its keys. Best-effort
        updates the key's `last_used_at`.
        """

    @abstractmethod
    async def delete_api_key(self, username: str, key_id: str) -> bool:
        """Revoke one of `username`'s keys; False if not found/owned."""

    @abstractmethod
    async def close(self) -> None: ...


class SqliteUserStore(UserStore):
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        path = Path(self._db_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        # Additive migration: the token-limit columns are nullable (NULL =
        # inherit default), so existing rows upgrade cleanly.
        cur = await self._db.execute("PRAGMA table_info(users)")
        cols = {row["name"] for row in await cur.fetchall()}
        for column in ("daily_token_limit", "monthly_token_limit"):
            if column not in cols:
                await self._db.execute(
                    f"ALTER TABLE users ADD COLUMN {column} INTEGER"
                )
        if "email" not in cols:
            await self._db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        # User-generated API keys (one row per key, hash-only at rest).
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_used_at REAL
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_username ON api_keys(username)"
        )
        await self._db.commit()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not initialized; call init() first")
        return self._db

    @staticmethod
    def _record(row: aiosqlite.Row) -> UserRecord:
        return UserRecord(
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            disabled=bool(row["disabled"]),
            created_at=row["created_at"],
            daily_token_limit=row["daily_token_limit"],
            monthly_token_limit=row["monthly_token_limit"],
            email=row["email"],
        )

    async def get(self, username: str) -> UserRecord | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT username, password_hash, role, disabled, created_at, "
                "daily_token_limit, monthly_token_limit, email "
                "FROM users WHERE username = ?",
                (username,),
            )
            row = await cur.fetchone()
        return self._record(row) if row is not None else None

    async def exists(self, username: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            )
            return await cur.fetchone() is not None

    async def list_users(self) -> list[UserOut]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT username, role, disabled, created_at, "
                "daily_token_limit, monthly_token_limit, email "
                "FROM users ORDER BY created_at DESC"
            )
            rows = await cur.fetchall()
        return [
            UserOut(
                username=r["username"],
                role=r["role"],
                disabled=bool(r["disabled"]),
                created_at=r["created_at"],
                daily_token_limit=r["daily_token_limit"],
                monthly_token_limit=r["monthly_token_limit"],
                email=r["email"],
            )
            for r in rows
        ]

    async def create(
        self, username: str, password_hash: str, role: str, *, disabled: bool = False
    ) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "INSERT OR IGNORE INTO users "
                "(username, password_hash, role, disabled, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, password_hash, role, 1 if disabled else 0, time.time()),
            )
            await db.commit()
            return cur.rowcount > 0

    async def _update(self, column: str, value: object, username: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                f"UPDATE users SET {column} = ? WHERE username = ?",
                (value, username),
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_password(self, username: str, password_hash: str) -> bool:
        return await self._update("password_hash", password_hash, username)

    async def set_role(self, username: str, role: str) -> bool:
        return await self._update("role", role, username)

    async def set_email(self, username: str, email: str | None) -> bool:
        return await self._update("email", email, username)

    async def get_by_email(self, email: str) -> UserRecord | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT username, password_hash, role, disabled, created_at, "
                "daily_token_limit, monthly_token_limit, email "
                "FROM users WHERE email IS NOT NULL "
                "AND LOWER(email) = LOWER(?) ORDER BY created_at ASC LIMIT 1",
                (email,),
            )
            row = await cur.fetchone()
        return self._record(row) if row is not None else None

    async def rename(self, old: str, new: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT 1 FROM users WHERE username = ?", (new,)
            )
            if await cur.fetchone() is not None:
                return False  # target name already taken
            cur = await db.execute(
                "UPDATE users SET username = ? WHERE username = ?", (new, old)
            )
            await db.execute(
                "UPDATE api_keys SET username = ? WHERE username = ?", (new, old)
            )
            await db.commit()
            return cur.rowcount > 0

    async def set_disabled(self, username: str, disabled: bool) -> bool:
        return await self._update("disabled", 1 if disabled else 0, username)

    async def set_token_limits(
        self, username: str, *, daily: int | None, monthly: int | None
    ) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "UPDATE users SET daily_token_limit = ?, monthly_token_limit = ? "
                "WHERE username = ?",
                (daily, monthly, username),
            )
            await db.commit()
            return cur.rowcount > 0

    async def delete(self, username: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "DELETE FROM users WHERE username = ?", (username,)
            )
            await db.execute(
                "DELETE FROM api_keys WHERE username = ?", (username,)
            )
            await db.commit()
            return cur.rowcount > 0

    async def seed(self, entries: list[tuple[str, str, str]]) -> None:
        if not entries:
            return
        now = time.time()
        async with self._lock:
            db = self._conn()
            await db.executemany(
                "INSERT OR IGNORE INTO users "
                "(username, password_hash, role, disabled, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                [(u, h, r, now) for u, h, r in entries],
            )
            await db.commit()

    async def create_api_key(
        self, username: str, key_hash: str, key_prefix: str, name: str
    ) -> str:
        key_id = secrets.token_hex(8)
        async with self._lock:
            db = self._conn()
            await db.execute(
                "INSERT INTO api_keys "
                "(id, username, key_hash, key_prefix, name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, username, key_hash, key_prefix, name, time.time()),
            )
            await db.commit()
        return key_id

    async def list_api_keys(self, username: str) -> list[ApiKeyRecord]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT id, username, key_prefix, name, created_at, last_used_at "
                "FROM api_keys WHERE username = ? ORDER BY created_at DESC",
                (username,),
            )
            rows = await cur.fetchall()
        return [
            ApiKeyRecord(
                id=r["id"],
                username=r["username"],
                key_prefix=r["key_prefix"],
                name=r["name"],
                created_at=r["created_at"],
                last_used_at=r["last_used_at"],
            )
            for r in rows
        ]

    async def resolve_api_key(self, key_hash: str) -> str | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT k.username FROM api_keys k JOIN users u "
                "ON u.username = k.username "
                "WHERE k.key_hash = ? AND u.disabled = 0",
                (key_hash,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await db.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                (time.time(), key_hash),
            )
            await db.commit()
        return row["username"]

    async def delete_api_key(self, username: str, key_id: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "DELETE FROM api_keys WHERE id = ? AND username = ?",
                (key_id, username),
            )
            await db.commit()
            return cur.rowcount > 0

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
