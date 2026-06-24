"""Search provider abstraction.

Every backend implements `SearchProvider.search()` and returns a normalized
list of `SearchResult` objects so the rest of the system never has to know
which engine produced them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from app.schemas import SearchResult


class SearchError(RuntimeError):
    """Raised when a backend is misconfigured or a request fails."""


class SearchProvider(ABC):
    #: stable identifier used in config and the API (e.g. "brave").
    name: str = "base"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @abstractmethod
    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        """Run a search and return up to `num` normalized results."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this backend has the credentials/config it needs."""
