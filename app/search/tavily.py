"""Tavily search API backend (LLM-oriented search)."""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider

ENDPOINT = "https://api.tavily.com/search"


class TavilyProvider(SearchProvider):
    name = "tavily"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._key = settings.tavily_api_key

    def is_configured(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("TAVILY_API_KEY is not set")
        resp = await self._client.post(
            ENDPOINT,
            json={
                "api_key": self._key,
                "query": query,
                "max_results": min(num, 20),
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for item in data.get("results", [])[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source=self.name,
                    score=item.get("score"),
                )
            )
        return out
