"""Search backend registry: lookup, listing, and multi-backend aggregation."""
from __future__ import annotations

import asyncio

import httpx

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchError, SearchProvider
from app.search.brave import BraveProvider
from app.search.duckduckgo import DuckDuckGoProvider
from app.search.google import GoogleProvider
from app.search.perplexity import PerplexityProvider
from app.search.searxng import SearxngProvider
from app.search.serper import SerperProvider
from app.search.tavily import TavilyProvider

_PROVIDER_CLASSES = [
    SearxngProvider,
    BraveProvider,
    TavilyProvider,
    SerperProvider,
    PerplexityProvider,
    GoogleProvider,
    DuckDuckGoProvider,
]


class SearchRegistry:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._settings = settings
        self._providers: dict[str, SearchProvider] = {
            cls.name: cls(client, settings) for cls in _PROVIDER_CLASSES
        }

    def get(self, name: str) -> SearchProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise SearchError(f"unknown search backend: {name!r}")
        return provider

    def available(self) -> list[str]:
        """Names of backends that have the config they need."""
        return [n for n, p in self._providers.items() if p.is_configured()]

    def all_names(self) -> list[str]:
        return list(self._providers)

    async def search(self, query: str, backend: str, num: int = 10) -> list[SearchResult]:
        return await self.get(backend).search(query, num=num)

    async def aggregate(
        self, query: str, backends: list[str] | None = None, num: int = 10
    ) -> list[SearchResult]:
        """Query several backends concurrently and merge, deduping by URL."""
        names = backends or self.available()
        configured = [n for n in names if self._providers[n].is_configured()]
        if not configured:
            raise SearchError("no configured search backends to aggregate")

        tasks = [self.get(n).search(query, num=num) for n in configured]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[SearchResult] = []
        seen: set[str] = set()
        for result in gathered:
            if isinstance(result, BaseException):
                continue  # skip a backend that errored; others still count
            for item in result:
                key = item.url or item.snippet
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged
