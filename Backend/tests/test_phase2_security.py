import pytest

from app.services.async_cache import LoopScopedTTLCache


@pytest.mark.asyncio
async def test_loop_cache_rejects_empty_and_upstream_error_values():
    cache = LoopScopedTTLCache(ttl=60, maxsize=4)

    cache.set("none", None)
    cache.set("empty", {})
    cache.set("error", {"status_code": 500, "error": "upstream unavailable"})

    assert cache.get("none") is None
    assert cache.get("empty") is None
    assert cache.get("error") is None


@pytest.mark.asyncio
async def test_loop_cache_evicts_least_recently_used_entry():
    cache = LoopScopedTTLCache(ttl=60, maxsize=2)
    cache.set("first", 1)
    cache.set("second", 2)
    assert cache.get("first") == 1
    cache.set("third", 3)

    assert cache.get("first") == 1
    assert cache.get("second") is None
    assert cache.get("third") == 3
