import asyncio
import json
import time
from collections import OrderedDict
from functools import wraps
from weakref import WeakKeyDictionary

from async_lru import alru_cache


class LoopScopedTTLCache:
    """Small in-memory TTL cache whose entries are isolated per event loop."""

    def __init__(self, *, ttl: float = 24 * 60 * 60, maxsize: int = 1000):
        if ttl <= 0:
            raise ValueError("Cache TTL must be positive.")
        if maxsize <= 0:
            raise ValueError("Cache maxsize must be positive.")
        self.ttl = ttl
        self.maxsize = maxsize
        self._entries = WeakKeyDictionary()

    def get(self, key):
        loop = asyncio.get_running_loop()
        entries = self._entries.get(loop)
        if entries is None:
            return None
        cached = entries.get(key)
        if cached is None:
            return None
        expires_at, value = cached
        if expires_at <= time.monotonic():
            entries.pop(key, None)
            return None
        entries.move_to_end(key)
        return value

    def set(self, key, value):
        if (
            value is None
            or value == {}
            or (
                isinstance(value, dict)
                and (value.get("error") or str(value.get("status_code", "")) in {"404", "500"})
            )
        ):
            return
        loop = asyncio.get_running_loop()
        entries = self._entries.setdefault(loop, OrderedDict())
        entries[key] = (time.monotonic() + self.ttl, value)
        entries.move_to_end(key)
        while len(entries) > self.maxsize:
            entries.popitem(last=False)

    def set_json(self, key, value):
        if value is None or value == {}:
            return
        if isinstance(value, dict) and (
            value.get("error") or str(value.get("status_code", "")) in {"404", "500"}
        ):
            return
        self.set(key, json.dumps(value, ensure_ascii=False))

    def invalidate(self, key):
        for entries in tuple(self._entries.values()):
            entries.pop(key, None)


def loop_scoped_alru_cache(*, maxsize: int, ttl: float | None = None):
    effective_ttl = 24 * 60 * 60 if ttl is None else ttl
    if effective_ttl <= 0:
        raise ValueError("Cache TTL must be positive.")

    def decorate(function):
        cache_registry_attribute = "_grace_async_caches"

        @wraps(function)
        async def cached(*args, **kwargs):
            loop = asyncio.get_running_loop()
            loop_caches = getattr(loop, cache_registry_attribute, None)
            if loop_caches is None:
                loop_caches = {}
                setattr(loop, cache_registry_attribute, loop_caches)
            loop_cache = loop_caches.get(function)
            if loop_cache is None:
                loop_cache = alru_cache(maxsize=maxsize, ttl=effective_ttl)(function)
                loop_caches[function] = loop_cache
            result = await loop_cache(*args, **kwargs)
            if (
                result is None
                or result == {}
                or (
                    isinstance(result, dict)
                    and (result.get("error") or str(result.get("status_code", "")) in {"404", "500"})
                )
            ):
                loop_cache.cache_clear()
            return result

        return cached

    return decorate
