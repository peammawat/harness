"""OpenAI-compatible provider.

Used for both real OpenAI ("openai") and any OpenAI-compatible local server
("local", e.g. Ollama / vLLM) — the only difference is base_url / api_key /
default model, supplied at construction.
"""
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


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        settings: Settings,
        *,
        name: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        supports_vision: bool = True,
    ) -> None:
        self.name = name
        self._default_model = default_model or settings.default_openai_model
        self._supports_vision = supports_vision
        self._client = None
        if api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def is_configured(self) -> bool:
        return self._client is not None

    # --- conversion ------------------------------------------------------

    def _to_native(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "assistant":
                if msg.raw is not None:
                    out.append(msg.raw)
                elif msg.tool_calls:
                    out.append(
                        {
                            "role": "assistant",
                            "content": msg.content or None,
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.name,
                                        "arguments": json.dumps(c.arguments),
                                    },
                                }
                                for c in msg.tool_calls
                            ],
                        }
                    )
                else:
                    out.append({"role": "assistant", "content": msg.content})
            elif msg.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )
            elif msg.role == "user" and msg.images:
                if self._supports_vision:
                    parts: list[dict[str, Any]] = []
                    if msg.content:
                        parts.append({"type": "text", "text": msg.content})
                    for img in msg.images:
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{img.media_type};base64,{img.data}"
                                },
                            }
                        )
                    out.append({"role": "user", "content": parts})
                else:
                    # Non-vision model: drop the image data but tell the model
                    # something was attached so the conversation stays coherent.
                    note = f"[ผู้ใช้แนบรูปภาพ {len(msg.images)} รูป ซึ่งโมเดลนี้ไม่รองรับการดูภาพ]"
                    content = f"{msg.content}\n\n{note}" if msg.content else note
                    out.append({"role": "user", "content": content})
            else:  # system / user (text only)
                out.append({"role": msg.role, "content": msg.content})
        return out

    @staticmethod
    def _tools_native(tools: list[ToolDef]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
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
        assert self._client is not None, "openai provider not configured"
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": self._to_native(messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = self._tools_native(tools)

        # Accumulate tool-call fragments by index across stream chunks.
        acc: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        finish_reason = "stop"

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                yield TextDelta(delta.content)
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        calls: list[ToolCall] = []
        for _, slot in sorted(acc.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))

        if calls:
            raw_assistant = {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ],
            }
            yield ToolUseRequest(calls=calls, raw_assistant=raw_assistant)
            yield TurnEnd(stop_reason="tool_calls")
        else:
            yield TurnEnd(stop_reason=finish_reason)
