"""Perplexity backend.

Perplexity exposes a chat-completions API whose `sonar` models return an
answer plus `search_results`/`citations`. We surface those as SearchResult
rows (the synthesized answer becomes the first result).
"""
from __future__ import annotations

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider

ENDPOINT = "https://api.perplexity.ai/chat/completions"


class PerplexityProvider(SearchProvider):
    name = "perplexity"

    def __init__(self, client, settings: Settings) -> None:
        super().__init__(client)
        self._key = settings.perplexity_api_key

    def is_configured(self) -> bool:
        return bool(self._key)

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        if not self.is_configured():
            raise SearchError("PERPLEXITY_API_KEY is not set")
        resp = await self._client.post(
            ENDPOINT,
            json={
                "model": "sonar",
                "messages": [{"role": "user", "content": query}],
            },
            headers={"Authorization": f"Bearer {self._key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []

        choices = data.get("choices") or []
        if choices:
            answer = choices[0].get("message", {}).get("content", "")
            if answer:
                out.append(
                    SearchResult(
                        title="Perplexity answer",
                        url="",
                        snippet=answer,
                        source=self.name,
                    )
                )

        # Newer responses include structured search_results; older ones only citations.
        for item in (data.get("search_results") or [])[:num]:
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    source=self.name,
                )
            )
        if len(out) <= 1:
            for url in (data.get("citations") or [])[:num]:
                out.append(SearchResult(title=url, url=url, source=self.name))
        return out[: num + 1]
