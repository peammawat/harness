"""Search registry: configured-listing and aggregate dedupe."""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.schemas import SearchResult
from app.search.base import SearchProvider
from app.search.registry import SearchRegistry


class _Fake(SearchProvider):
    def __init__(self, name, results):
        self.name = name
        self._results = results

    def is_configured(self):
        return True

    async def search(self, query, *, num=10):
        return self._results


@pytest.mark.asyncio
async def test_aggregate_dedupes_by_url():
    async with httpx.AsyncClient() as client:
        reg = SearchRegistry(client, Settings(_env_file=None))
        reg._providers = {
            "a": _Fake("a", [SearchResult(title="A", url="https://dup.com")]),
            "b": _Fake(
                "b",
                [
                    SearchResult(title="B-dup", url="https://dup.com"),
                    SearchResult(title="B-new", url="https://new.com"),
                ],
            ),
        }
        merged = await reg.aggregate("q", backends=["a", "b"])
        urls = [r.url for r in merged]
        assert urls == ["https://dup.com", "https://new.com"]


@pytest.mark.asyncio
async def test_duckduckgo_always_configured():
    async with httpx.AsyncClient() as client:
        reg = SearchRegistry(client, Settings(_env_file=None))
        assert "duckduckgo" in reg.available()
        assert "brave" not in reg.available()  # no key set
