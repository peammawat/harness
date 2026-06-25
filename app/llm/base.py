"""LLM provider abstraction.

Each provider streams a turn and yields a sequence of *normalized* events so
the agent loop never needs to know which SDK produced them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


# --- Normalized message + tool types -------------------------------------

@dataclass
class ImagePart:
    """An image attached to a user turn, sent to the model as a vision block."""

    media_type: str  # "image/png" | "image/jpeg" | "image/webp" | "image/gif"
    data: str  # base64-encoded bytes, no "data:" prefix


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    # For assistant turns that requested tools:
    tool_calls: list["ToolCall"] = field(default_factory=list)
    # For tool-result messages:
    tool_call_id: str | None = None
    # Provider-native assistant payload, kept verbatim so multi-turn tool use
    # round-trips correctly (e.g. Anthropic thinking blocks). Only ever
    # consumed by the provider that produced it (provider is fixed per chat).
    raw: Any = None
    # Images attached to a user turn. Translated to provider-native vision
    # blocks by each provider's `_to_native`. Only meaningful on user turns;
    # never collides with `raw` (which is assistant-only).
    images: list["ImagePart"] = field(default_factory=list)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


# --- Normalized streaming events ------------------------------------------

@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseRequest:
    calls: list[ToolCall]
    # Provider-native assistant content for this turn (passed back unchanged on
    # the next turn). Stored on the assistant Message's `raw` by the agent loop.
    raw_assistant: Any = None


@dataclass
class TurnEnd:
    stop_reason: str  # "end_turn" | "tool_use" | other
    # Token usage for this turn (0 when the provider/server doesn't report it).
    input_tokens: int = 0
    output_tokens: int = 0


LLMEvent = TextDelta | ToolUseRequest | TurnEnd


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        force_tool: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream one model turn as normalized events.

        When ``force_tool`` is set, the provider must require the model to call
        that specific tool this turn (provider-native ``tool_choice``).
        """
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)
