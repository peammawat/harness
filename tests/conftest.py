"""Shared test fakes."""
from __future__ import annotations

from collections.abc import AsyncIterator

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
from app.schemas import SearchResult


class FakeSearchRegistry:
    """Stand-in for SearchRegistry used by the agent loop."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._results = results or [
            SearchResult(title="Example", url="https://example.com", snippet="hi", source="fake")
        ]

    async def search(self, query: str, backend: str, num: int = 10) -> list[SearchResult]:
        self.calls.append((query, backend, num))
        return self._results


class FakeProvider(LLMProvider):
    """Calls web_search on the first turn, answers on the second."""

    name = "fake"

    def is_configured(self) -> bool:
        return True

    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        force_tool: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        already_searched = any(m.role == "tool" for m in messages)
        if not already_searched and tools:
            yield TextDelta("Let me search. ")
            yield ToolUseRequest(
                calls=[ToolCall(id="t1", name="web_search", arguments={"query": "x"})],
                raw_assistant={"role": "assistant", "content": "Let me search."},
            )
            yield TurnEnd(stop_reason="tool_use", input_tokens=10, output_tokens=20)
        else:
            yield TextDelta("Final answer.")
            yield TurnEnd(stop_reason="end_turn", input_tokens=10, output_tokens=20)


class FakeFetchProvider(LLMProvider):
    """Calls fetch_url on the first turn, answers on the second."""

    name = "fake-fetch"

    def __init__(self, url: str = "https://example.com") -> None:
        self._url = url

    def is_configured(self) -> bool:
        return True

    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        force_tool: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        already_fetched = any(m.role == "tool" for m in messages)
        if not already_fetched and tools:
            yield TextDelta("Reading. ")
            yield ToolUseRequest(
                calls=[ToolCall(id="f1", name="fetch_url", arguments={"url": self._url})],
                raw_assistant={"role": "assistant", "content": "Reading."},
            )
            yield TurnEnd(stop_reason="tool_use")
        else:
            yield TextDelta("Done reading.")
            yield TurnEnd(stop_reason="end_turn")


class RecordingProvider(LLMProvider):
    """Answers immediately, recording the tools and system prompts it was given."""

    name = "recording"

    def __init__(self) -> None:
        self.tools_seen: list[str] = []
        self.system_seen: list[str] = []
        self.force_tool_seen: list[str | None] = []

    def is_configured(self) -> bool:
        return True

    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str | None = None,
        max_tokens: int = 16000,
        force_tool: str | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.tools_seen = [t.name for t in tools]
        self.system_seen = [m.content for m in messages if m.role == "system"]
        self.force_tool_seen.append(force_tool)
        yield TextDelta("ok")
        yield TurnEnd(stop_reason="end_turn")
