"""Google Programmable Search Engine (Custom Search JSON API) backend."""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider

ENDPOINT = "https://www.googleapis.com/customsearch/v1"


class GoogleProvider(SearchProvider):
    name = "google"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._key = settings.google_api_key
        self._cse = settings.google_cse_id

    def is_configured(self) -> bool:
        return bool(self._key and self._cse)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("GOOGLE_API_KEY and GOOGLE_CSE_ID must both be set")
        # The CSE API caps `num` at 10 per request.
        resp = await self._client.get(
            ENDPOINT,
            params={
                "key": self._key,
                "cx": self._cse,
                "q": query,
                "num": min(num, 10),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for item in data.get("items", [])[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source=self.name,
                )
            )
        return out
