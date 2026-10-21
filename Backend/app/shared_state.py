"""Shared, multi-process-safe rate limiting and short-lived caching.

The previous implementation kept rate-limit buckets and response caches in
plain in-process dicts. That works for a single uvicorn worker on a single
machine, but silently stops providing any real protection the moment the
app runs with more than one worker or more than one replica (each process
gets its own dict, so a user can simply get routed to a "fresh" bucket).

This module provides one async-friendly interface, backed by Redis when
`settings.redis_url` is configured, and falling back to the previous
in-memory behavior otherwise (with a loud one-time warning so it's obvious
in logs that the deployment isn't actually shared-state-safe).
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict

from app.config import settings
from app.errors import SharedStateUnavailable
from app.observability import logger

try:
    from redis import asyncio as redis_asyncio  # type: ignore
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - redis is an optional dependency
    redis_asyncio = None
    RedisError = ConnectionError

REDIS_FAILURES = (RedisError, ConnectionError, OSError, TimeoutError)


class _InMemoryBackend:
    """Best-effort fallback. Not safe across multiple processes/instances."""

    def __init__(self):
        self._buckets: dict[str, list[float]] = {}
        self._cache: OrderedDict[str, tuple[float, object, int | None]] = OrderedDict()

    async def allow(self, key: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(self._buckets) > 5000:
            for k in list(self._buckets)[:1000]:
                self._buckets.pop(k, None)
        return True

    async def cache_get(self, key: str, ttl: int):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < min(ttl, hit[2] or ttl):
            return hit[1]
        self._cache.pop(key, None)
        return None

    async def cache_set(self, key: str, value, max_entries: int, ttl: int | None = None):
        self._cache[key] = (time.monotonic(), value, ttl)
        self._cache.move_to_end(key)
        while len(self._cache) > max_entries:
            self._cache.popitem(last=False)

    async def claim(self, key: str, ttl: int) -> bool:
        marker = f"claim:{key}"
        now = time.monotonic()
        hit = self._cache.get(marker)
        if hit and now - hit[0] < (hit[2] or ttl):
            return False
        self._cache[marker] = (now, True, ttl)
        return True

    async def release(self, key: str) -> None:
        self._cache.pop(f"claim:{key}", None)


class _RedisBackend:
    def __init__(self, url: str):
        self._client = redis_asyncio.from_url(url, decode_responses=False)

    async def allow(self, key: str, limit: int, window: int) -> bool:
        redis_key = f"ratelimit:{key}"
        script = (
            "local count = redis.call('INCR', KEYS[1]); "
            "if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]); end; "
            "return count"
        )
        count = await self._client.eval(script, 1, redis_key, window)
        return count <= limit

    async def ping(self):
        return await self._client.ping()

    async def cache_get(self, key: str, ttl: int):
        raw = await self._client.get(f"cache:{key}")
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return payload.get("value") if payload.get("version") == 1 else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    async def cache_set(self, key: str, value, max_entries: int, ttl: int | None = None):
        await self._client.set(
            f"cache:{key}",
            json.dumps({"version": 1, "value": value}, separators=(",", ":")),
            ex=ttl or settings.cache_ttl_seconds,
        )

    async def claim(self, key: str, ttl: int) -> bool:
        return bool(await self._client.set(f"claim:{key}", b"1", ex=ttl, nx=True))

    async def release(self, key: str) -> None:
        await self._client.delete(f"claim:{key}")


class SharedState:
    """Facade used by the rest of the app. Picks a backend once at import."""

    # After falling back to memory, Redis is tried again this often instead of
    # never: one network blip used to leave a worker on private in-memory
    # state (rate limits, guest chats, working state) until it restarted.
    REDIS_RETRY_SECONDS = 30.0

    def __init__(self):
        self._ready = not settings.is_production
        self._redis: _RedisBackend | None = None
        self._redis_retry_at = 0.0
        if settings.redis_url and redis_asyncio is not None:
            self._backend = _RedisBackend(settings.redis_url)
            self._redis = self._backend
            logger.info("shared_state_backend backend=redis")
        else:
            if settings.is_production:
                raise RuntimeError("Production requires REDIS_URL and the redis package.")
            if settings.redis_url and redis_asyncio is None:
                logger.warning(
                    "shared_state_redis_unavailable "
                    "REDIS_URL is set but the redis package is not installed; "
                    "falling back to in-memory state."
                )
            elif settings.is_production:
                logger.warning(
                    "shared_state_backend backend=memory "
                    "Running in production without REDIS_URL: rate limiting and "
                    "caching are per-process only and will not be consistent "
                    "across multiple workers/instances."
                )
            self._backend = _InMemoryBackend()

    def _fallback_to_memory(self, reason: str):
        self._redis_retry_at = time.monotonic() + self.REDIS_RETRY_SECONDS
        if isinstance(self._backend, _InMemoryBackend):
            return
        logger.warning(
            "shared_state_redis_unavailable %s; falling back to in-memory state.",
            reason,
        )
        self._backend = _InMemoryBackend()

    async def _maybe_recover(self) -> None:
        """Return to Redis once it answers again."""
        redis = getattr(self, "_redis", None)
        if redis is None or self._backend is redis:
            return
        if time.monotonic() < getattr(self, "_redis_retry_at", 0.0):
            return
        self._redis_retry_at = time.monotonic() + self.REDIS_RETRY_SECONDS
        try:
            await redis.ping()
        except REDIS_FAILURES as exc:
            logger.warning("shared_state_redis_still_unavailable %r", exc)
            return
        logger.warning("shared_state_redis_recovered; using Redis again.")
        self._backend = redis

    async def check_ready(self):
        if isinstance(self._backend, _RedisBackend):
            try:
                await self._backend.ping()
            except REDIS_FAILURES as exc:  # pragma: no cover - exercised by runtime/staging failure drills
                self._ready = False
                logger.warning("shared_state_not_ready %s", exc)
                return False
        self._ready = True
        return True

    @property
    def ready(self):
        return self._ready

    async def rate_limit(self, key: str, limit: int, window: int = 60) -> bool:
        await self._maybe_recover()
        try:
            return await self._backend.allow(key, limit, window)
        except REDIS_FAILURES as exc:  # pragma: no cover - exercised by runtime/staging failure drills
            if settings.is_production:
                logger.error("security_rate_limit_unavailable key=%s error=%r", key, exc)
                raise SharedStateUnavailable() from exc
            self._fallback_to_memory(str(exc))
            return await self._backend.allow(key, limit, window)

    async def cache_get(self, key: str, ttl: int):
        await self._maybe_recover()
        try:
            return await self._backend.cache_get(key, ttl)
        except REDIS_FAILURES as exc:  # pragma: no cover - exercised by runtime fallback
            self._fallback_to_memory(str(exc))
            return await self._backend.cache_get(key, ttl)

    async def cache_set(self, key: str, value, max_entries: int, ttl: int | None = None):
        await self._maybe_recover()
        try:
            await self._backend.cache_set(key, value, max_entries, ttl)
        except REDIS_FAILURES as exc:  # pragma: no cover - exercised by runtime fallback
            self._fallback_to_memory(str(exc))
            await self._backend.cache_set(key, value, max_entries, ttl)

    async def claim(self, key: str, ttl: int = 300) -> bool:
        await self._maybe_recover()
        try:
            return await self._backend.claim(key, ttl)
        except REDIS_FAILURES as exc:  # pragma: no cover - exercised by runtime/staging failure drills
            if settings.is_production:
                logger.error("shared_state_claim_unavailable key=%s error=%r", key, exc)
                raise SharedStateUnavailable() from exc
            self._fallback_to_memory(str(exc))
            return await self._backend.claim(key, ttl)

    async def release(self, key: str) -> None:
        """End a claim early (the claimed work finished); expiry still applies if this fails."""
        try:
            await self._backend.release(key)
        except REDIS_FAILURES as exc:  # pragma: no cover - the claim expires on its own
            logger.warning("shared_state_release_failed key=%s error=%r", key, exc)


shared_state = SharedState()
