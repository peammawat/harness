"""User store: DB-backed accounts for web login + roles.

Holds the credentials and role for each managed user. Seeded once from
`AUTH_USERS` on startup; the admin panel is the source of truth afterwards.
Mirrors `SqliteConversationStore`: one shared `aiosqlite` connection guarded by
an `asyncio.Lock` (single-node). Swap in a Redis/Postgres impl of this ABC for
scale-out — nothing else changes.
"""
from __future__ import annotations

import asyncio
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
    async def set_disabled(self, username: str, disabled: bool) -> bool: ...

    @abstractmethod
    async def delete(self, username: str) -> bool: ...

    @abstractmethod
    async def seed(self, entries: list[tuple[str, str, str]]) -> None:
        """Insert (username, password_hash, role) rows, skipping existing ones."""

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
        )

    async def get(self, username: str) -> UserRecord | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT username, password_hash, role, disabled, created_at "
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
                "SELECT username, role, disabled, created_at "
                "FROM users ORDER BY created_at DESC"
            )
            rows = await cur.fetchall()
        return [
            UserOut(
                username=r["username"],
                role=r["role"],
                disabled=bool(r["disabled"]),
                created_at=r["created_at"],
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

    async def set_disabled(self, username: str, disabled: bool) -> bool:
        return await self._update("disabled", 1 if disabled else 0, username)

    async def delete(self, username: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "DELETE FROM users WHERE username = ?", (username,)
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

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
