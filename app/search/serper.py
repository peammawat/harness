"""Serper.dev backend (Google results via API)."""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider

ENDPOINT = "https://google.serper.dev/search"


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._key = settings.serper_api_key

    def is_configured(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("SERPER_API_KEY is not set")
        resp = await self._client.post(
            ENDPOINT,
            json={"q": query, "num": min(num, 20)},
            headers={"X-API-KEY": self._key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for item in data.get("organic", [])[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source=self.name,
                    score=float(item["position"]) if "position" in item else None,
                )
            )
        return out
