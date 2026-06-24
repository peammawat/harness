"""Agent loop: tool round-trips, fetch_url, deep research, SSRF guard."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.agent.loop import run_agent
from app.config import Settings
from app.llm.base import Message, TextDelta, TurnEnd
from tests.conftest import (
    FakeFetchProvider,
    FakeProvider,
    FakeSearchRegistry,
    RecordingProvider,
)


async def _collect(agen):
    return [e async for e in agen]


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def _run(provider, search, **kwargs):
    """run_agent with the http_client/settings boilerplate filled in."""
    async with httpx.AsyncClient() as client:
        return await _collect(
            run_agent(
                provider=provider,
                search_registry=search,
                http_client=client,
                settings=kwargs.pop("settings", _settings()),
                messages=kwargs.pop("messages", [Message(role="user", content="hi")]),
                search_backend=kwargs.pop("search_backend", "duckduckgo"),
                **kwargs,
            )
        )


@pytest.mark.asyncio
async def test_agent_runs_tool_then_answers():
    search = FakeSearchRegistry()
    events = await _run(
        FakeProvider(), search, messages=[Message(role="user", content="hello")]
    )
    types = [e["type"] for e in events]

    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"

    done = events[-1]
    assert "Final answer." in done["content"]
    assert done["tool_calls"] == 1
    assert search.calls and search.calls[0][1] == "duckduckgo"


@pytest.mark.asyncio
async def test_agent_without_search_skips_tools():
    search = FakeSearchRegistry()
    events = await _run(FakeProvider(), search, enable_search=False)
    types = [e["type"] for e in events]
    assert "tool_call" not in types
    assert types[-1] == "done"
    assert search.calls == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_url_round_trip():
    respx.get("https://example.com").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Hi</title></head><body><p>Hello world</p></body></html>",
        )
    )
    search = FakeSearchRegistry()
    events = await _run(FakeFetchProvider("https://example.com"), search)

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["name"] == "fetch_url"
    assert results[0]["url"] == "https://example.com"
    assert results[0]["chars"] > 0
    assert "error" not in results[0]
    assert events[-1]["type"] == "done"
    assert "Done reading." in events[-1]["content"]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_url_caps_chars():
    big = "<html><body><p>" + ("x" * 5000) + "</p></body></html>"
    respx.get("https://example.com").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=big)
    )
    search = FakeSearchRegistry()
    events = await _run(
        FakeFetchProvider("https://example.com"), search,
        settings=_settings(fetch_max_chars=100),
    )
    result = [e for e in events if e["type"] == "tool_result"][0]
    assert result["chars"] <= 100


@pytest.mark.asyncio
async def test_fetch_url_blocks_private_host():
    search = FakeSearchRegistry()
    events = await _run(FakeFetchProvider("http://169.254.169.254/latest/meta-data"), search)
    result = [e for e in events if e["type"] == "tool_result"][0]
    # The guard refuses the fetch; the error is surfaced, not raised.
    assert result["name"] == "fetch_url"
    assert "error" in result
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_deep_research_enables_fetch_and_skill():
    provider = RecordingProvider()
    search = FakeSearchRegistry()
    await _run(provider, search, deep_research=True)
    # fetch_url is exposed and the deep-research skill is the leading system msg.
    assert "fetch_url" in provider.tools_seen
    assert "web_search" in provider.tools_seen
    assert provider.system_seen and "Deep Research" in provider.system_seen[0]


class _AlwaysToolProvider(FakeProvider):
    """Requests web_search every turn that tools are offered; answers only when
    tools are withheld (i.e. the forced final turn)."""

    name = "always-tool"

    async def stream_turn(self, messages, tools, *, model=None, max_tokens=16000):
        from app.llm.base import ToolCall, ToolUseRequest

        if tools:
            yield TextDelta("searching ")
            yield ToolUseRequest(
                calls=[ToolCall(id="t", name="web_search", arguments={"query": "x"})],
                raw_assistant={"role": "assistant", "content": "searching"},
            )
            yield TurnEnd(stop_reason="tool_use")
        else:
            yield TextDelta("Forced final answer.")
            yield TurnEnd(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_loop_forces_answer_at_iteration_cap():
    search = FakeSearchRegistry()
    events = await _run(
        _AlwaysToolProvider(), search, settings=_settings(max_iterations=3)
    )
    types = [e["type"] for e in events]
    # No dead-end error; the loop ends with a real answer.
    assert "error" not in types
    assert types[-1] == "done"
    assert "Forced final answer." in events[-1]["content"]
    # The final turn withholds tools, so we run the tool at most cap-1 times.
    assert events[-1]["tool_calls"] == 2


@pytest.mark.asyncio
async def test_normal_chat_omits_fetch_tool_in_skill():
    provider = RecordingProvider()
    search = FakeSearchRegistry()
    await _run(provider, search, deep_research=False, enable_search=True)
    # fetch_url is still available (so pasted links work), but the skill is the
    # regular search skill, not the deep-research one.
    assert "fetch_url" in provider.tools_seen
    assert provider.system_seen and "Deep Research" not in provider.system_seen[0]
