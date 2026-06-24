"""SQLite-backed `ConversationStore` (single-node).

Only message text is persisted (`role` + `content`); inline attachments
(images / documents, base64) are intentionally not stored — they are large and
not the substance of the history.

A single shared `aiosqlite` connection is guarded by an `asyncio.Lock`, which is
sufficient for a single-node deployment. For multi-worker scale-out, provide a
different `ConversationStore` implementation (Redis/Postgres).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import aiosqlite

from app.schemas import ConversationDetail, ConversationSummary, StoredMessage
from app.storage.base import ConversationStore


class SqliteConversationStore(ConversationStore):
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
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_user "
            "ON conversations (user, updated_at DESC)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
            "ON messages (conversation_id)"
        )
        await self._db.commit()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not initialized; call init() first")
        return self._db

    async def create_conversation(self, user: str, title: str) -> str:
        conversation_id = uuid.uuid4().hex
        now = time.time()
        async with self._lock:
            db = self._conn()
            await db.execute(
                "INSERT INTO conversations (id, user, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, user, title, now, now),
            )
            await db.commit()
        return conversation_id

    async def append_message(
        self, user: str, conversation_id: str, role: str, content: str
    ) -> None:
        now = time.time()
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ? AND user = ?",
                (now, conversation_id, user),
            )
            if cur.rowcount == 0:
                # Unknown conversation or not owned by this user — drop silently.
                await db.rollback()
                return
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, now),
            )
            await db.commit()

    async def list_conversations(self, user: str) -> list[ConversationSummary]:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user = ?
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """,
                (user,),
            )
            rows = await cur.fetchall()
        return [
            ConversationSummary(
                id=r["id"],
                title=r["title"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                message_count=r["message_count"],
            )
            for r in rows
        ]

    async def get_conversation(
        self, user: str, conversation_id: str
    ) -> ConversationDetail | None:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE id = ? AND user = ?",
                (conversation_id, user),
            )
            conv = await cur.fetchone()
            if conv is None:
                return None
            cur = await db.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id ASC",
                (conversation_id,),
            )
            msg_rows = await cur.fetchall()
        return ConversationDetail(
            id=conv["id"],
            title=conv["title"],
            created_at=conv["created_at"],
            updated_at=conv["updated_at"],
            messages=[
                StoredMessage(
                    role=m["role"], content=m["content"], created_at=m["created_at"]
                )
                for m in msg_rows
            ],
        )

    async def delete_conversation(self, user: str, conversation_id: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "DELETE FROM conversations WHERE id = ? AND user = ?",
                (conversation_id, user),
            )
            if cur.rowcount == 0:
                await db.rollback()
                return False
            await db.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
            await db.commit()
        return True

    async def owns(self, user: str, conversation_id: str) -> bool:
        async with self._lock:
            db = self._conn()
            cur = await db.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND user = ?",
                (conversation_id, user),
            )
            return await cur.fetchone() is not None

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
