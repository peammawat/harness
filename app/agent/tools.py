"""Tool definitions exposed to the agent (web_search, fetch_url)."""
from __future__ import annotations

from app.llm.base import ToolDef

WEB_SEARCH = ToolDef(
    name="web_search",
    description=(
        "Search the web for current or factual information. Call this whenever "
        "the answer depends on recent events, specific facts, prices, or anything "
        "not reliably known from training data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "How many results to return (default 8).",
            },
        },
        "required": ["query"],
    },
)


FETCH_URL = ToolDef(
    name="fetch_url",
    description=(
        "Fetch a web page and return its readable text content. Use this to "
        "read a page in full when a search snippet is not enough, or whenever "
        "the user gives you a link to read."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL to fetch.",
            },
        },
        "required": ["url"],
    },
)


def default_tools() -> list[ToolDef]:
    return [WEB_SEARCH]


def research_tools(*, search: bool, fetch: bool) -> list[ToolDef]:
    """Assemble the tool set for a chat turn from feature flags."""
    tools: list[ToolDef] = []
    if search:
        tools.append(WEB_SEARCH)
    if fetch:
        tools.append(FETCH_URL)
    return tools
