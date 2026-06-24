"""Brave Search API backend."""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider(SearchProvider):
    name = "brave"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._key = settings.brave_api_key

    def is_configured(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("BRAVE_API_KEY is not set")
        resp = await self._client.get(
            ENDPOINT,
            params={"q": query, "count": min(num, 20)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._key,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for item in data.get("web", {}).get("results", [])[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    source=self.name,
                )
            )
        return out
