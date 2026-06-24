"""DuckDuckGo backend via the `ddgs` library (no API key required).

`ddgs` is synchronous, so we run it in a worker thread to stay async-friendly.
"""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"

    def __init__(self, client, settings: Settings) -> None:  # client unused
        super().__init__(client)

    def is_configured(self) -> bool:
        return True  # keyless

    @staticmethod
    def _blocking_search(query: str, num: int) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover
            raise SearchError("the `ddgs` package is not installed") from exc
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=num))

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        rows = await asyncio.to_thread(self._blocking_search, query, num)
        out: list[SearchResult] = []
        for item in rows[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("href", "") or item.get("url", ""),
                    snippet=item.get("body", "") or item.get("snippet", ""),
                    source=self.name,
                )
            )
        return out
