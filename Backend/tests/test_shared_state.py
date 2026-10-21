import asyncio

import pytest

from app.shared_state import SharedState, _InMemoryBackend


@pytest.mark.asyncio
async def test_rate_limit_allows_up_to_the_limit_then_blocks():
    backend = _InMemoryBackend()
    for _ in range(3):
        assert await backend.allow("user:1", limit=3, window=60) is True
    assert await backend.allow("user:1", limit=3, window=60) is False


@pytest.mark.asyncio
async def test_rate_limit_is_isolated_per_key():
    backend = _InMemoryBackend()
    for _ in range(3):
        assert await backend.allow("user:1", limit=3, window=60) is True
    # A different key has its own independent bucket.
    assert await backend.allow("user:2", limit=3, window=60) is True


@pytest.mark.asyncio
async def test_rate_limit_window_expires_old_entries(monkeypatch):
    backend = _InMemoryBackend()
    fake_now = [1000.0]
    monkeypatch.setattr("app.shared_state.time.monotonic", lambda: fake_now[0])

    for _ in range(3):
        assert await backend.allow("user:1", limit=3, window=10) is True
    assert await backend.allow("user:1", limit=3, window=10) is False

    fake_now[0] += 11  # advance past the window
    assert await backend.allow("user:1", limit=3, window=10) is True


@pytest.mark.asyncio
async def test_cache_get_returns_none_before_any_set():
    backend = _InMemoryBackend()
    assert await backend.cache_get("missing", ttl=60) is None


@pytest.mark.asyncio
async def test_cache_set_then_get_roundtrips():
    backend = _InMemoryBackend()
    await backend.cache_set("key", {"value": 42}, max_entries=100)
    assert await backend.cache_get("key", ttl=60) == {"value": 42}


@pytest.mark.asyncio
async def test_cache_entry_expires_after_ttl(monkeypatch):
    backend = _InMemoryBackend()
    fake_now = [2000.0]
    monkeypatch.setattr("app.shared_state.time.monotonic", lambda: fake_now[0])

    await backend.cache_set("key", "value", max_entries=100)
    assert await backend.cache_get("key", ttl=30) == "value"

    fake_now[0] += 31
    assert await backend.cache_get("key", ttl=30) is None
    assert "key" not in backend._cache


@pytest.mark.asyncio
async def test_cache_evicts_oldest_entries_past_max_entries():
    backend = _InMemoryBackend()
    for i in range(5):
        await backend.cache_set(f"key{i}", i, max_entries=3)
    # Only the 3 most recently written entries should remain.
    assert await backend.cache_get("key0", ttl=60) is None
    assert await backend.cache_get("key1", ttl=60) is None
    assert await backend.cache_get("key4", ttl=60) == 4


@pytest.mark.asyncio
async def test_shared_state_falls_back_to_memory_without_redis_url(monkeypatch):
    from app import shared_state as module

    monkeypatch.setattr(module.settings, "redis_url", None)
    state = SharedState()
    assert isinstance(state._backend, _InMemoryBackend)
    assert await state.rate_limit("k", limit=1, window=60) is True
    assert await state.rate_limit("k", limit=1, window=60) is False


@pytest.mark.asyncio
async def test_shared_state_rate_limit_runs_concurrently_without_error():
    # Guards against accidental non-async-safe shared mutable state bugs
    # (e.g. iterating a dict while another coroutine mutates it).
    state = SharedState()
    state._backend = _InMemoryBackend()
    results = await asyncio.gather(*(state.rate_limit(f"k{i % 5}", limit=50, window=60) for i in range(100)))
    assert all(results)


@pytest.mark.asyncio
async def test_redis_failure_fails_closed_for_production_rate_limits(monkeypatch):
    from app import shared_state as module

    monkeypatch.setattr(module.settings, "environment", "production")

    class FailedRedisBackend:
        async def allow(self, *_args, **_kwargs):
            raise ConnectionError("redis is unavailable")

    state = SharedState.__new__(SharedState)
    state._backend = FailedRedisBackend()
    state._ready = False

    from app.errors import SharedStateUnavailable

    with pytest.raises(SharedStateUnavailable):
        await state.rate_limit("secure-limit", limit=1, window=60)
    assert isinstance(state._backend, FailedRedisBackend)


@pytest.mark.asyncio
async def test_redis_failure_can_still_degrade_noncritical_cache_to_memory(monkeypatch):
    from app import shared_state as module

    monkeypatch.setattr(module.settings, "environment", "development")

    class FailedRedisBackend:
        async def cache_get(self, *_args, **_kwargs):
            raise ConnectionError("redis is unavailable")

        async def cache_set(self, *_args, **_kwargs):
            raise ConnectionError("redis is unavailable")

    state = SharedState.__new__(SharedState)
    state._backend = FailedRedisBackend()
    state._ready = True

    assert await state.cache_get("fallback", 60) is None
    assert isinstance(state._backend, _InMemoryBackend)


async def test_shared_state_does_not_hide_unexpected_errors(monkeypatch):
    import app.shared_state as module

    monkeypatch.setattr(module.settings, "redis_url", None)
    state = SharedState()

    class BrokenBackend:
        async def allow(self, *_args, **_kwargs):
            raise ValueError("invalid backend state")

    backend = BrokenBackend()
    state._backend = backend

    with pytest.raises(ValueError, match="invalid backend state"):
        await state.rate_limit("k", limit=1, window=60)
    assert state._backend is backend


@pytest.mark.asyncio
async def test_after_a_redis_outage_the_state_returns_to_redis(monkeypatch):
    """A blip must not leave a worker on private in-memory state until it restarts."""
    from app import shared_state as module

    monkeypatch.setattr(module.settings, "environment", "development")

    class FlakyRedis:
        def __init__(self):
            self.up = False
            self.store = {}

        async def ping(self):
            if not self.up:
                raise ConnectionError("down")
            return True

        async def cache_get(self, key, _ttl):
            if not self.up:
                raise ConnectionError("down")
            return self.store.get(key)

        async def cache_set(self, key, value, _max_entries, ttl=None):
            if not self.up:
                raise ConnectionError("down")
            self.store[key] = value

    redis = FlakyRedis()
    state = SharedState.__new__(SharedState)
    state._backend = redis
    state._redis = redis
    state._redis_retry_at = 0.0
    state._ready = True

    await state.cache_set("k", 1, 10)  # Redis down: falls back to memory
    assert isinstance(state._backend, _InMemoryBackend)
    redis.up = True
    state._redis_retry_at = 0.0  # the retry interval has passed
    await state.cache_set("k", 2, 10)
    assert state._backend is redis and redis.store["k"] == 2
