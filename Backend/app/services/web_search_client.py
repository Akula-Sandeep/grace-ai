from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import ClassVar, Protocol

import httpx

from app.config import settings


class WebSearchProvider(Protocol):
    async def search(self, query: str): ...
    async def close(self): ...


class GeminiGroundedSearchProvider:
    async def search(self, query: str):
        # Gemini grounded search, then Groq browser search when it fails (services/web_search.py).
        from app.services.web_search import search

        evidence = await search(query)
        if not evidence or not evidence.get("summary"):
            raise RuntimeError("Web search returned no verified sources.")
        details = evidence.get("source_details") or [
            {
                "url": url,
                "title": "Search source",
                "source_domain": None,
                "supported_text": "",
            }
            for url in evidence.get("sources") or []
        ]
        results = [
            {
                "title": item.get("title") or "Search source",
                "url": item.get("url"),
                "source_domain": item.get("source_domain"),
                # The text this source actually supports; fall back to the
                # overall grounded answer when Gemini gave no segment mapping.
                "snippet": item.get("supported_text") or evidence["summary"],
            }
            for item in details
            if item.get("url")
        ]
        return {
            "query": query,
            "summary": evidence["summary"],
            "sources": [item["url"] for item in results],
            "results": results,
        }

    async def close(self):
        return None


class WebSearchClient:
    _cache: ClassVar[OrderedDict] = OrderedDict()
    _locks: ClassVar[dict] = {}

    def __init__(self, provider: WebSearchProvider | None = None):
        self.provider = provider

    @classmethod
    def _trim_locks(cls):
        if len(cls._locks) <= settings.cache_max_entries:
            return
        for key, lock in list(cls._locks.items()):
            if len(cls._locks) <= settings.cache_max_entries:
                break
            if not lock.locked():
                cls._locks.pop(key, None)

    async def search(self, query):
        if self.provider is None:
            raise RuntimeError("Web search provider is not configured.")
        key = " ".join(query.casefold().split())
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < settings.cache_ttl_seconds:
            return hit[1]
        self._trim_locks()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            hit = self._cache.get(key)
            if hit and now - hit[0] < settings.cache_ttl_seconds:
                return hit[1]
            try:
                result = await self.provider.search(query)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    return {
                        "query": query,
                        "summary": "Web search is temporarily rate-limited. Please try again shortly.",
                        "sources": [],
                        "results": [],
                        "fallback": "rate_limited",
                    }
                raise
            self._cache[key] = (time.monotonic(), result)
            self._cache.move_to_end(key)
            while len(self._cache) > settings.cache_max_entries:
                self._cache.popitem(last=False)
            self._trim_locks()
            return result

    async def close(self):
        close = getattr(self.provider, "close", None)
        if close:
            await close()
