"""Provider-agnostic agent loop.

Drives an LLM provider through a tool-use loop, executing the `web_search`
and `fetch_url` tools, and yields SSE-friendly event dicts:

    {"type": "token",       "text": "..."}
    {"type": "tool_call",   "name": "web_search", "arguments": {...}}
    {"type": "tool_result", "name": "web_search", "results": [...]}
    {"type": "tool_result", "name": "fetch_url", "url": "...", "chars": N}
    {"type": "done",        "content": "<full answer>", "tool_calls": N,
     "input_tokens": N, "output_tokens": N}
    {"type": "error",       "message": "..."}
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.agent.fetch import FetchError, html_to_text, validate_url
from app.agent.skills import deep_research_skill, search_skill
from app.agent.tools import research_tools
from app.config import Settings
from app.llm.base import (
    LLMProvider,
    Message,
    TextDelta,
    ToolDef,
    ToolUseRequest,
    TurnEnd,
)
from app.search.base import SearchError
from app.search.registry import SearchRegistry


async def _run_web_search(
    registry: SearchRegistry, backend: str, arguments: dict
) -> list[dict]:
    query = arguments.get("query", "")
    num = int(arguments.get("num_results", 8) or 8)
    results = await registry.search(query, backend=backend, num=num)
    return [r.model_dump() for r in results]


async def _run_fetch_url(
    client: httpx.AsyncClient, settings: Settings, arguments: dict
) -> dict:
    """Fetch a URL and return ``{url, title, text}`` or ``{url, error}``.

    Guards against SSRF (scheme + private-IP checks, re-validated after any
    redirect) and caps both downloaded bytes and returned characters.
    """
    raw_url = (arguments.get("url") or "").strip()
    url = validate_url(raw_url, block_private_ips=settings.fetch_block_private_ips)
    resp = await client.get(
        url,
        follow_redirects=True,
        timeout=settings.fetch_timeout,
        headers={"User-Agent": settings.fetch_user_agent},
    )
    # Re-validate the final host (a redirect could point at an internal address).
    # Residual risk: DNS is resolved independently at validate_url time and at
    # connect time, so a TOCTOU/DNS-rebinding attacker controlling a domain could
    # in principle return a public IP to the check and a private IP to the actual
    # connect. Fully closing this needs pinning the validated IP and connecting to
    # it directly (custom transport); accepted as a known limitation for now.
    validate_url(str(resp.url), block_private_ips=settings.fetch_block_private_ips)
    resp.raise_for_status()

    ctype = resp.headers.get("content-type", "")
    body = resp.content[: settings.fetch_max_bytes]
    if "html" in ctype:
        encoding = resp.encoding or "utf-8"
        title, text = html_to_text(body.decode(encoding, errors="replace"))
    elif "text/" in ctype or "json" in ctype or not ctype:
        title, text = "", body.decode("utf-8", errors="replace")
    else:
        return {"url": url, "error": f"unsupported content-type: {ctype}"}

    text = text[: settings.fetch_max_chars]
    return {"url": url, "title": title, "text": text}


async def run_agent(
    *,
    provider: LLMProvider,
    search_registry: SearchRegistry,
    http_client: httpx.AsyncClient,
    settings: Settings,
    messages: list[Message],
    search_backend: str,
    enable_search: bool = True,
    deep_research: bool = False,
    model: str | None = None,
    max_tokens: int = 16000,
) -> AsyncIterator[dict]:
    # fetch_url is available whenever search is on (so a pasted link is always
    # readable); deep research only changes the skill and the iteration budget.
    tools: list[ToolDef] = research_tools(
        search=enable_search, fetch=(deep_research or enable_search)
    )
    convo = list(messages)
    # Lead with the relevant research skill so the model follows the methodology.
    if deep_research:
        skill = deep_research_skill()
    elif enable_search:
        skill = search_skill()
    else:
        skill = ""
    if skill:
        convo.insert(0, Message(role="system", content=skill))
    full_text: list[str] = []
    tool_calls_made = 0
    total_input_tokens = 0
    total_output_tokens = 0

    max_iters = (
        settings.deep_research_iterations if deep_research else settings.max_iterations
    )
    # Whenever search is available (search enabled, or deep research), force a web
    # search on the first turn so the model can't skip it and answer from stale
    # training data. Turn 0 is never the last turn, so this never collides with the
    # final-turn tool withholding below.
    force_search = any(t.name == "web_search" for t in tools)
    for i in range(max_iters):
        # On the final permitted turn, withhold tools and nudge the model so it
        # produces an answer from what it already gathered instead of
        # dead-ending on the iteration cap.
        last_turn = i == max_iters - 1
        turn_tools: list[ToolDef] = [] if last_turn else tools
        force_tool = "web_search" if (i == 0 and force_search) else None
        if last_turn and tool_calls_made:
            convo.append(
                Message(
                    role="system",
                    content=(
                        "You have reached the research step limit. Answer the "
                        "user now using the information already gathered; do not "
                        "request any more tools."
                    ),
                )
            )

        tool_request: ToolUseRequest | None = None

        async for event in provider.stream_turn(
            convo, turn_tools, model=model, max_tokens=max_tokens,
            force_tool=force_tool,
        ):
            if isinstance(event, TextDelta):
                full_text.append(event.text)
                yield {"type": "token", "text": event.text}
            elif isinstance(event, ToolUseRequest):
                tool_request = event
            elif isinstance(event, TurnEnd):
                total_input_tokens += event.input_tokens
                total_output_tokens += event.output_tokens

        if tool_request is None:
            break  # model produced a final answer

        # Append the assistant turn (native blocks preserved via raw).
        convo.append(
            Message(
                role="assistant",
                content="".join(full_text),
                tool_calls=tool_request.calls,
                raw=tool_request.raw_assistant,
            )
        )

        # Execute each requested tool and feed results back.
        for call in tool_request.calls:
            yield {"type": "tool_call", "id": call.id, "name": call.name,
                   "arguments": call.arguments}
            if call.name == "web_search":
                try:
                    results = await _run_web_search(
                        search_registry, search_backend, call.arguments
                    )
                    tool_calls_made += 1
                    yield {"type": "tool_result", "id": call.id,
                           "name": call.name, "results": results}
                    content = json.dumps(results, ensure_ascii=False)
                except SearchError as exc:
                    content = json.dumps({"error": str(exc)})
                    yield {"type": "tool_result", "id": call.id,
                           "name": call.name, "error": str(exc)}
                except Exception as exc:  # noqa: BLE001 — surface backend failures to the model
                    content = json.dumps({"error": f"search failed: {exc}"})
                    yield {"type": "tool_result", "id": call.id,
                           "name": call.name, "error": str(exc)}
            elif call.name == "fetch_url":
                try:
                    result = await _run_fetch_url(http_client, settings, call.arguments)
                    tool_calls_made += 1
                    if result.get("error"):
                        yield {"type": "tool_result", "id": call.id,
                               "name": call.name, "url": result.get("url", ""),
                               "error": result["error"]}
                    else:
                        # Compact summary to the UI; full text goes to the model.
                        yield {"type": "tool_result", "id": call.id,
                               "name": call.name, "url": result["url"],
                               "title": result.get("title", ""),
                               "chars": len(result.get("text", ""))}
                    content = json.dumps(result, ensure_ascii=False)
                except FetchError as exc:
                    content = json.dumps({"error": str(exc)})
                    yield {"type": "tool_result", "id": call.id,
                           "name": call.name, "error": str(exc)}
                except Exception as exc:  # noqa: BLE001 — surface fetch failures to the model
                    content = json.dumps({"error": f"fetch failed: {exc}"})
                    yield {"type": "tool_result", "id": call.id,
                           "name": call.name, "error": str(exc)}
            else:
                content = json.dumps({"error": f"unknown tool {call.name}"})

            convo.append(
                Message(role="tool", tool_call_id=call.id, content=content)
            )

    yield {
        "type": "done",
        "content": "".join(full_text),
        "tool_calls": tool_calls_made,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }
