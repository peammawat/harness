"""Usage store: per-request LLM token accounting.

One row per chat request (input/output token counts), keyed by the
authenticated identity. Powers the admin panel's per-user totals + recent list
+ daily trend, and each user's own usage view. Same single-connection +
`asyncio.Lock` shape as the other SQLite stores.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from pathlib import Path

import aiosqlite

from app.schemas import UsageEventOut, UsageSeriesPoint, UsageTotals

_DAY = 86400.0


class UsageStore(ABC):
    @abstractmethod
    async def init(self) -> None: ...

    @abstractmethod
    async def record(
        self,
        *,
        user: str,
        conversation_id: str | None,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Persist one usage event."""

    @abstractmethod
    async def totals(self) -> list[UsageTotals]:
        """Per-user totals across all users (admin view)."""

    @abstractmethod
    async def user_totals(self, user: str) -> UsageTotals:
        """Totals for a single user (zeros if none)."""

    @abstractmethod
    async def series(self, user: str, since: float) -> list[UsageSeriesPoint]:
        """Daily-bucketed totals for `user` from `since` (epoch seconds)."""

    @abstractmethod
    async def recent(self, user: str | None, limit: int) -> list[UsageEventOut]:
        """Most recent events, optionally filtered to one user."""

    @abstractmethod
    async def close(self) -> None: ...


class SqliteUsageStore(UsageStore):
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
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                conversation_id TEXT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_user_time "
            "ON usage_events (user, created_at)"
        )
        await self._db.commit()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not initialized; call init() first")
        return self._db

    async def record(
        self,
        *,
        user: str,
        conversation_id: str | None,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        async with self._lock:
            db = self._conn()
            await db.execute(
                "INSERT INTO usage_events (user, conversation_id, provider, model, "
                "input_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user,
                    conversation_id,
                    provider,
                    model,
                    int(input_tokens),
                    int(output_tokens),
                    time.time(),
                ),
            )
            await db.commit()

    async def totals(self) -> list[UsageTotals]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                """
                SELECT user,
                       SUM(input_tokens)  AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       COUNT(*)           AS events,
                       MAX(created_at)    AS last_used
                FROM usage_events
                GROUP BY user
                ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
                """
            )
            rows = await cur.fetchall()
        return [
            UsageTotals(
                user=r["user"],
                input_tokens=r["input_tokens"] or 0,
                output_tokens=r["output_tokens"] or 0,
                events=r["events"] or 0,
                last_used=r["last_used"],
            )
            for r in rows
        ]

    async def user_totals(self, user: str) -> UsageTotals:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                """
                SELECT SUM(input_tokens)  AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       COUNT(*)           AS events,
                       MAX(created_at)    AS last_used
                FROM usage_events WHERE user = ?
                """,
                (user,),
            )
            r = await cur.fetchone()
        return UsageTotals(
            user=user,
            input_tokens=(r["input_tokens"] or 0) if r else 0,
            output_tokens=(r["output_tokens"] or 0) if r else 0,
            events=(r["events"] or 0) if r else 0,
            last_used=r["last_used"] if r else None,
        )

    async def series(self, user: str, since: float) -> list[UsageSeriesPoint]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                """
                SELECT CAST(created_at / ? AS INTEGER) * ? AS day,
                       SUM(input_tokens)  AS input_tokens,
                       SUM(output_tokens) AS output_tokens
                FROM usage_events
                WHERE user = ? AND created_at >= ?
                GROUP BY day
                ORDER BY day
                """,
                (_DAY, _DAY, user, since),
            )
            rows = await cur.fetchall()
        return [
            UsageSeriesPoint(
                day=r["day"],
                input_tokens=r["input_tokens"] or 0,
                output_tokens=r["output_tokens"] or 0,
            )
            for r in rows
        ]

    async def recent(self, user: str | None, limit: int) -> list[UsageEventOut]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                """
                SELECT user, conversation_id, provider, model,
                       input_tokens, output_tokens, created_at
                FROM usage_events
                WHERE (? IS NULL OR user = ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user, user, limit),
            )
            rows = await cur.fetchall()
        return [
            UsageEventOut(
                user=r["user"],
                conversation_id=r["conversation_id"],
                provider=r["provider"],
                model=r["model"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
