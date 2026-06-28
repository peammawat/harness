"""Settings store: DB-backed runtime app settings (key/value).

Holds toggles that admins can flip at runtime without restarting the app
(env config is loaded once and immutable). Mirrors `SqliteUserStore`: one shared
`aiosqlite` connection guarded by an `asyncio.Lock` (single-node), living in the
same SQLite file as users + usage. Swap in a Redis/Postgres impl of this ABC for
scale-out — nothing else changes.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from pathlib import Path

import aiosqlite


class SettingsStore(ABC):
    @abstractmethod
    async def init(self) -> None:
        """Prepare the backing store (create tables). Idempotent."""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Return the stored string value, or None when unset."""

    @abstractmethod
    async def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    async def get_bool(self, key: str, default: bool) -> bool:
        """Return the stored value as a bool, falling back to `default`."""

    @abstractmethod
    async def set_bool(self, key: str, value: bool) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class SqliteSettingsStore(SettingsStore):
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
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not initialized; call init() first")
        return self._db

    async def get(self, key: str) -> str | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
        return row["value"] if row is not None else None

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            db = self._conn()
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, time.time()),
            )
            await db.commit()

    async def get_bool(self, key: str, default: bool) -> bool:
        raw = await self.get(key)
        if raw is None:
            return default
        return raw == "1"

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set(key, "1" if value else "0")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
