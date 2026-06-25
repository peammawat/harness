"""Conversation-store abstraction.

A `ConversationStore` persists chat history partitioned by user identity (the
value returned by `require_auth` — a username for web login, or the API key for
programmatic access). Every method takes the `user` so isolation is enforced at
the storage layer: a query can only ever touch rows owned by that user.

Swapping SQLite for Redis/Postgres later means one new implementation of this
ABC plus one line where it's constructed (`app/main.py`); nothing else changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas import ConversationDetail, ConversationSummary


class ConversationStore(ABC):
    @abstractmethod
    async def init(self) -> None:
        """Prepare the backing store (create tables, etc.). Idempotent."""

    @abstractmethod
    async def create_conversation(self, user: str, title: str) -> str:
        """Create a new conversation for `user` and return its id."""

    @abstractmethod
    async def append_message(
        self, user: str, conversation_id: str, role: str, content: str
    ) -> None:
        """Append one message and bump the conversation's `updated_at`.

        No-op if the conversation does not exist or is not owned by `user`.
        """

    @abstractmethod
    async def list_conversations(self, user: str) -> list[ConversationSummary]:
        """Return `user`'s conversations, most recently updated first."""

    @abstractmethod
    async def get_conversation(
        self, user: str, conversation_id: str
    ) -> ConversationDetail | None:
        """Return the conversation with its messages, or None if not owned."""

    @abstractmethod
    async def delete_conversation(self, user: str, conversation_id: str) -> bool:
        """Delete a conversation; return False if not owned / not found."""

    @abstractmethod
    async def owns(self, user: str, conversation_id: str) -> bool:
        """Whether `conversation_id` exists and belongs to `user`."""

    @abstractmethod
    async def set_share_token(self, user: str, conversation_id: str) -> str | None:
        """Enable public sharing and return the token (existing one if already set).

        Returns None if the conversation does not exist or is not owned by `user`.
        """

    @abstractmethod
    async def clear_share_token(self, user: str, conversation_id: str) -> bool:
        """Disable public sharing (revoke the link); return False if not owned."""

    @abstractmethod
    async def get_shared_conversation(self, token: str) -> ConversationDetail | None:
        """Return the conversation for a share `token` (no user check), or None.

        Read-only public access — bypasses user isolation by design, but is scoped
        to the single conversation that token unlocks.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources (connections)."""
