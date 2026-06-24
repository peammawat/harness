"""Anthropic (Claude) provider — official SDK, adaptive thinking, streaming."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.config import Settings
from app.llm.base import (
    LLMEvent,
    LLMProvider,
    Message,
    TextDelta,
    ToolCall,
    ToolDef,
    ToolUseRequest,
    TurnEnd,
)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._default_model = settings.default_anthropic_model
        self._client = None
        if settings.anthropic_api_key:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    def is_configured(self) -> bool:
        return self._client is not None

    # --- message conversion ---------------------------------------------

    @staticmethod
    def _to_native(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        system: str | None = None
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                system = (system + "\n\n" + msg.content) if system else msg.content
            elif msg.role == "assistant":
                if msg.raw is not None:
                    # Native content blocks (incl. thinking) preserved verbatim.
                    out.append({"role": "assistant", "content": msg.raw})
                else:
                    out.append({"role": "assistant", "content": msg.content})
            elif msg.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            else:  # user
                if msg.images:
                    blocks: list[dict[str, Any]] = []
                    if msg.content:
                        blocks.append({"type": "text", "text": msg.content})
                    for img in msg.images:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": img.media_type,
                                    "data": img.data,
                                },
                            }
                        )
                    out.append({"role": "user", "content": blocks})
                else:
                    out.append({"role": "user", "content": msg.content})
        return system, out

    @staticmethod
    def _tools_native(tools: list[ToolDef]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    # --- streaming -------------------------------------------------------

    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
    ) -> AsyncIterator[LLMEvent]:
        assert self._client is not None, "anthropic provider not configured"
        system, native_msgs = self._to_native(messages)

        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "messages": native_msgs,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._tools_native(tools)

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if (
                    event.type == "content_block_delta"
                    and getattr(event.delta, "type", None) == "text_delta"
                ):
                    yield TextDelta(event.delta.text)
            final = await stream.get_final_message()

        calls: list[ToolCall] = []
        for block in final.content:
            if block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))

        if calls:
            # Pass the native content blocks back unchanged next turn.
            raw_blocks = [b.model_dump() for b in final.content]
            yield ToolUseRequest(calls=calls, raw_assistant=raw_blocks)
        yield TurnEnd(stop_reason=final.stop_reason or "end_turn")
