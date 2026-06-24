"""Searxng (self-hosted meta-search) backend."""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider


class SearxngProvider(SearchProvider):
    name = "searxng"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._url = (settings.searxng_url or "").rstrip("/")

    def is_configured(self) -> bool:
        return bool(self._url)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("SEARXNG_URL is not set")
        resp = await self._client.get(
            f"{self._url}/search",
            params={"q": query, "format": "json"},
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
