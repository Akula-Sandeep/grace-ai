from __future__ import annotations

import asyncio
import time
import weakref
from collections import OrderedDict
from typing import ClassVar

import httpx

from app.config import settings
from app.observability import TimedCall, logger
from app.services.async_cache import loop_scoped_alru_cache
from app.services.upstream_errors import (
    UpstreamNotFound,
    UpstreamTimeout,
    UpstreamUnavailable,
    classify_upstream_exception,
    describe_upstream_exception,
)


# TMDB gets its own loop-scoped connection pool, deliberately separate from
# app.services.http_client's shared client (which Wikidata and MusicBrainz
# use, unchanged, with http2=True).
#
# Live-provider testing (2026-10) found TMDB requests reliably timing out
# (~37s across retries) on one network path while passing in ~1.7s on
# another, with Groq and vision unaffected on the same host in the same
# runs, and basic TCP/TLS connectivity to TMDB sometimes succeeding despite
# the real client failing. That rules out DNS and raw reachability as the
# cause (both passed) and rules out a bare IPv4/IPv6 "Happy Eyeballs" gap:
# the installed anyio (4.15.1) already races IPv4/IPv6 addresses with a
# 250ms stagger inside httpcore's AutoBackend, so there is no missing
# fallback to add. What the passing "basic HTTPS" probe actually checks
# (scripts/diagnose_network.py's https_probe) is a raw TLS socket with no
# ALPN protocols offered, i.e. plain HTTP/1.1 -- categorically different
# from the real client, which negotiates ALPN h2 because the shared
# httpx.AsyncClient is built with http2=True. An HTTP/2 connection that the
# network path mishandled (e.g. a middlebox or VPN-absent route that drops
# or stalls h2 frames post-handshake) would show exactly this signature:
# TLS/TCP succeed, the plain HTTP/1.1 probe succeeds, yet the real h2
# request hangs until every retry's timeout is exhausted. Raising the
# per-call timeout (tmdb_details_timeout_seconds, batch 13) only made the
# failure take longer without fixing it, which is evidence against "needs
# more transfer time" and consistent with a connection/negotiation-layer
# stall that no amount of extra time resolves.
#
# The fix: give TMDB its own client pinned to HTTP/1.1 (http2=False).
# This does not touch the shared client (Wikidata/MusicBrainz keep
# http2=True) and does not touch Groq (its own SDK-managed client,
# unrelated to httpx entirely). It is a code-side mitigation for a
# network-path problem that cannot be fully diagnosed or fixed from
# inside this repository -- see the module docstring note below and the
# progress doc for what remains environment-dependent.
def tmdb_breaker():
    from app.services.resilience import breaker

    return breaker("tmdb", threshold=2, network_rest=20.0)


def _record_tmdb_failure(exc: BaseException) -> None:
    from app.services.provider_health import record_failure

    category = classify_upstream_exception(exc)
    tmdb_breaker().failure(category)
    record_failure("tmdb", "api", exc)


_tmdb_clients_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def _new_tmdb_client() -> httpx.AsyncClient:
    kwargs: dict = {
        "http2": False,
        "limits": httpx.Limits(max_keepalive_connections=20, max_connections=40),
        "timeout": settings.tmdb_timeout_seconds,
    }
    # Optional, owner-configured outbound proxy for TMDB only (see
    # settings.tmdb_proxy_url). Never defaulted, never applied to any other
    # provider's client, and never logged here or anywhere else -- httpx
    # itself never includes proxy credentials in exception messages this
    # client's logging reads from (describe_upstream_exception/
    # classify_upstream_exception read status codes and transport-level
    # reasons, not the proxy URL).
    if settings.tmdb_proxy_url:
        kwargs["proxy"] = settings.tmdb_proxy_url
    return httpx.AsyncClient(**kwargs)


def get_tmdb_shared_client() -> httpx.AsyncClient:
    """Return this event loop's TMDB-dedicated client, creating it if needed.

    Deliberately independent of app.services.http_client.get_shared_client():
    TMDB is the only caller of this function, so pinning it to HTTP/1.1 here
    cannot affect Wikidata, MusicBrainz, or any other consumer of the
    general-purpose shared client.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        client = _new_tmdb_client()
        return client

    client = _tmdb_clients_by_loop.get(loop)
    if client is None or client.is_closed:
        client = _new_tmdb_client()
        _tmdb_clients_by_loop[loop] = client
    return client


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
    _cache: ClassVar[OrderedDict] = OrderedDict()
    _locks: ClassVar[dict] = {}
    MOOD_KEYWORD_TERMS: ClassVar[dict[str, tuple[str, ...]]] = {
        "dark": ("dark", "darkness"),
        "coastal": ("coast", "coastal", "seaside", "ocean"),
        "mind-bending": ("mind-bending", "psychological", "surreal"),
        "atmospheric": ("atmosphere", "moody"),
        "surreal": ("surreal", "dream"),
        "cerebral": ("psychological", "philosophy"),
        "thriller": ("thriller", "suspense"),
        "action": ("action", "fight"),
        "romantic": ("romance", "love"),
        "funny": ("comedy", "humor"),
    }

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.headers, self._auth_params = self.credentials(settings.tmdb_api_key)
        self._client = client

    @staticmethod
    def credentials(key: str | None) -> tuple[dict[str, str], dict[str, str]]:
        """Return (headers, query params) for either TMDB credential type.

        TMDB issues a v4 "API Read Access Token" (a JWT, sent as a Bearer
        header) and a v3 "API Key" (32 hex characters, sent as ``api_key``).
        Both are commonly configured as TMDB_API_KEY.
        """
        key = (key or "").strip()
        headers = {"accept": "application/json"}
        if key.count(".") == 2 and key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
            return headers, {}
        return headers, ({"api_key": key} if key else {})

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client or get_tmdb_shared_client()

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @classmethod
    def _cache_put(cls, key, data):
        cls._cache[key] = (time.monotonic(), data)
        cls._cache.move_to_end(key)
        while len(cls._cache) > settings.cache_max_entries:
            cls._cache.popitem(last=False)

    @classmethod
    def _trim_locks(cls):
        if len(cls._locks) <= settings.cache_max_entries:
            return
        for key, lock in list(cls._locks.items()):
            if len(cls._locks) <= settings.cache_max_entries:
                break
            if not lock.locked():
                cls._locks.pop(key, None)

    async def _get(self, path, params=None, *, timeout_seconds: float | None = None):
        if not settings.tmdb_api_key:
            raise RuntimeError("TMDB_API_KEY is not configured.")
        per_attempt_timeout = (
            timeout_seconds if timeout_seconds is not None else settings.tmdb_timeout_seconds
        )
        key = (path, tuple(sorted((params or {}).items())))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < settings.cache_ttl_seconds:
            return hit[1]
        self._trim_locks()
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                hit = self._cache.get(key)
                now = time.monotonic()
                if hit and now - hit[0] < settings.cache_ttl_seconds:
                    return hit[1]
                started = time.monotonic()
                circuit = tmdb_breaker()
                if not circuit.allow():
                    # TMDB failed repeatedly a moment ago: answer at once instead of
                    # making every request in this turn wait out the same timeouts.
                    raise UpstreamUnavailable(
                        "TMDB is unreachable right now.",
                        provider="tmdb",
                        retry_after_seconds=circuit.retry_after() or 1,
                    )
                from app.core.deadline import DeadlineReached, time_left

                outer = time_left(per_attempt_timeout * 3 + 2.0, reserve=1.0)
                if outer <= 0:
                    raise UpstreamTimeout("No time left in this turn for TMDB.", provider="tmdb")
                try:
                    async with TimedCall("tmdb"):
                        # The outer deadline must cover every attempt
                        # _get_with_retries() can make (each bounded by its own
                        # per-request timeout below) plus their backoff sleeps,
                        # not just one attempt's worth of time -- otherwise the
                        # retry loop is cancelled before a second attempt ever
                        # starts. It is also never longer than the turn has left.
                        data = await asyncio.wait_for(
                            self._get_with_retries(path, params, per_attempt_timeout),
                            timeout=outer,
                        )
                except DeadlineReached as exc:
                    raise UpstreamTimeout("No time left in this turn for TMDB.", provider="tmdb") from exc
                except (TimeoutError, httpx.TimeoutException) as exc:
                    logger.warning(
                        "tmdb_request_timed_out path=%s per_attempt_timeout=%s "
                        "outer_deadline=%s elapsed=%.2fs detail=%s",
                        path,
                        per_attempt_timeout,
                        per_attempt_timeout * 3 + 2.0,
                        time.monotonic() - started,
                        describe_upstream_exception(exc),
                    )
                    _record_tmdb_failure(exc)
                    raise UpstreamTimeout("TMDB request timed out.", provider="tmdb") from exc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        circuit.success()
                        raise UpstreamNotFound("The requested movie was not found.", provider="tmdb") from exc
                    _record_tmdb_failure(exc)
                    raise UpstreamUnavailable(
                        "TMDB returned an unsuccessful response.", provider="tmdb"
                    ) from exc
                except httpx.NetworkError as exc:
                    _record_tmdb_failure(exc)
                    raise UpstreamUnavailable("TMDB could not be reached.", provider="tmdb") from exc
                if not isinstance(data, dict) or not data:
                    raise UpstreamUnavailable("TMDB returned an invalid response.", provider="tmdb")
                circuit.success()
                from app.services.provider_health import record_success

                record_success("tmdb", "api")
                self._cache_put(key, data)
                return data
        finally:
            self._trim_locks()

    async def _get_with_retries(self, path, params, per_attempt_timeout: float):
        # Connect (DNS + TCP + TLS handshake) and read (waiting for the
        # server to actually send a response) fail for different reasons.
        # A route that cannot reach TMDB at all typically fails or hangs at
        # connect well within a few seconds -- there is nothing to gain by
        # waiting as long as per_attempt_timeout for that, 3 times over
        # (observed: ~37s total for a provably dead route, all spent on a
        # connect-phase failure that could not have succeeded no matter how
        # long it waited). A connection that DID establish but is slow to
        # get a response is a different, more plausibly transient failure,
        # and keeps the full per_attempt_timeout budget.
        request_timeout = httpx.Timeout(
            connect=min(settings.tmdb_connect_timeout_seconds, per_attempt_timeout),
            read=per_attempt_timeout,
            write=per_attempt_timeout,
            pool=per_attempt_timeout,
        )
        for attempt in range(3):
            attempt_started = time.monotonic()
            try:
                response = await self.client.get(
                    self.BASE_URL + path,
                    headers=self.headers,
                    params={**(params or {}), **self._auth_params},
                    # Explicit per-request timeout: the shared httpx client's
                    # own default (used by other callers) must not silently
                    # decide how long a TMDB request is allowed to take.
                    timeout=request_timeout,
                )
                if response.status_code == 429:
                    if attempt == 2:
                        response.raise_for_status()
                    logger.info(
                        "tmdb_attempt_retrying path=%s attempt=%s status=429 elapsed=%.2fs",
                        path,
                        attempt + 1,
                        time.monotonic() - attempt_started,
                    )
                    await asyncio.sleep(min(float(response.headers.get("retry-after", "1")), 2))
                    continue
                if response.status_code >= 500:
                    if attempt == 2:
                        response.raise_for_status()
                    logger.info(
                        "tmdb_attempt_retrying path=%s attempt=%s status=%s elapsed=%.2fs",
                        path,
                        attempt + 1,
                        response.status_code,
                        time.monotonic() - attempt_started,
                    )
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                response.raise_for_status()
                return response.json()
            # Sanitized per-attempt timing and classification: which attempt
            # (of the up to 3 this loop makes) actually consumed the
            # overall deadline, and why, without ever logging the request
            # URL (TMDB's v3 key travels as a query parameter -- see
            # describe_upstream_exception's HTTPStatusError special case).
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                # A connect-phase failure (the TCP/TLS handshake itself
                # never completed or was refused) means this route could
                # not reach TMDB at all -- distinct from a read-phase
                # failure (the connection succeeded but no response came),
                # which is more plausibly transient. Retrying a dead route
                # a 3rd time at the same connect budget cannot succeed
                # where two identical attempts already failed, and is
                # exactly the "spend 37 seconds retrying an unreachable
                # route" failure mode this is meant to avoid -- so a
                # connect-phase failure gets one fewer attempt than a
                # read-phase one.
                is_connect_phase = isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
                logger.warning(
                    "tmdb_attempt_failed path=%s attempt=%s per_attempt_timeout=%s elapsed=%.2fs "
                    "category=%s connect_phase=%s detail=%s",
                    path,
                    attempt + 1,
                    per_attempt_timeout,
                    time.monotonic() - attempt_started,
                    classify_upstream_exception(exc),
                    is_connect_phase,
                    describe_upstream_exception(exc),
                )
                if attempt == 2 or (is_connect_phase and attempt == 1):
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("TMDB request failed")

    @loop_scoped_alru_cache(maxsize=100, ttl=settings.cache_ttl_seconds)
    async def search_movies(self, q: str, page: int = 1):
        return await self._get(
            "/search/movie",
            {"query": q, "language": "en-US", "include_adult": False, "page": page},
        )

    @loop_scoped_alru_cache(maxsize=100, ttl=settings.cache_ttl_seconds)
    async def get_movie_details(self, i: int):
        return await self._get(
            f"/movie/{i}",
            {
                "language": "en-US",
                "append_to_response": "credits,keywords,images,videos,reviews,release_dates,external_ids,translations,watch/providers,recommendations",
            },
            # This bundles nine sub-resources into one response via
            # append_to_response -- a much larger payload than any other
            # call this client makes -- so it gets a longer per-attempt
            # budget than the default tmdb_timeout_seconds (see
            # tmdb_details_timeout_seconds for why).
            timeout_seconds=settings.tmdb_details_timeout_seconds,
        )

    async def get_similar_movies(self, i, page=1):
        return await self._get(f"/movie/{i}/similar", {"language": "en-US", "page": page})

    async def get_recommendations(self, i, page=1):
        return await self._get(f"/movie/{i}/recommendations", {"language": "en-US", "page": page})

    @loop_scoped_alru_cache(maxsize=200, ttl=settings.cache_ttl_seconds)
    async def get_watch_providers(self, movie_id: int, region: str | None = None) -> dict:
        region = region or settings.default_region
        response = await self._get(f"/movie/{movie_id}/watch/providers")
        region_data = (response.get("results") or {}).get(region.upper(), {})
        providers = {
            "link": region_data.get("link"),
        }
        for category in ("flatrate", "rent", "buy"):
            providers[category] = [
                {
                    "provider_name": provider["provider_name"],
                    "logo_path": provider.get("logo_path"),
                }
                for provider in region_data.get(category, [])
                if provider.get("provider_name")
            ]
        return providers

    async def _keyword_ids_for_moods(self, mood_keywords):
        mood_terms = []
        for mood in mood_keywords:
            normalized = str(mood).strip().casefold()
            mood_terms.append(self.MOOD_KEYWORD_TERMS.get(normalized, (normalized,)))
        unique_terms = list(dict.fromkeys(term for terms in mood_terms for term in terms if term))
        responses = await asyncio.gather(
            *(self._get("/search/keyword", {"query": term, "page": 1}) for term in unique_terms)
        )
        results_by_term = dict(zip(unique_terms, responses, strict=True))
        keyword_groups = []
        for terms in mood_terms:
            keyword_ids = set()
            for term in terms:
                result = results_by_term.get(term, {})
                for keyword in result.get("results", []):
                    name = str(keyword.get("name", "")).strip().casefold()
                    if (name == term.casefold() or name.startswith(term.casefold() + " ")) and keyword.get(
                        "id"
                    ) is not None:
                        keyword_ids.add(int(keyword["id"]))
            if keyword_ids:
                keyword_groups.append(sorted(keyword_ids))
        return keyword_groups

    async def discover_movies(self, **kw):
        p = {
            "language": "en-US",
            "include_adult": False,
            "sort_by": kw.get("sort_by", "popularity.desc"),
            "page": kw.get("page", 1),
        }
        runtime_ceiling = None
        if kw.get("runtime_min") is not None:
            p["with_runtime.gte"] = kw["runtime_min"]
        if kw.get("runtime_max") is not None:
            runtime_ceiling = min(1000, kw["runtime_max"] + 15)
            p["with_runtime.lte"] = runtime_ceiling
        if kw.get("release_year_min") is not None:
            p["primary_release_date.gte"] = f"{kw['release_year_min']}-01-01"
        if kw.get("release_year_max") is not None:
            p["primary_release_date.lte"] = f"{kw['release_year_max']}-12-31"
        if kw.get("genre_ids"):
            p["with_genres"] = ",".join(map(str, kw["genre_ids"]))
        elif kw.get("any_genre_ids"):
            # Any of these genres (TMDB's "|" is OR); used to widen a mood's pool.
            p["with_genres"] = "|".join(map(str, kw["any_genre_ids"]))
        if kw.get("without_genre_ids"):
            p["without_genres"] = ",".join(map(str, kw["without_genre_ids"]))
        if kw.get("original_language"):
            p["with_original_language"] = kw["original_language"]
        if kw.get("vote_count_min") is not None:
            p["vote_count.gte"] = kw["vote_count_min"]
        if kw.get("vote_average_min") is not None:
            p["vote_average.gte"] = kw["vote_average_min"]
        if kw.get("with_keywords"):
            # A required keyword set (e.g. true stories) is a filter; moods then
            # only re-rank, because TMDB can't mix AND/OR keyword groups.
            p["with_keywords"] = kw["with_keywords"]
        elif kw.get("mood_keywords"):
            keyword_groups = await self._keyword_ids_for_moods(kw["mood_keywords"])
            if len(keyword_groups) != len(kw["mood_keywords"]):
                # An unmapped mood must not empty the result set: moods only
                # widen or bias discovery, they are never hard filters.
                logger.info(
                    "tmdb_mood_keyword_mapping_incomplete moods=%s",
                    ",".join(map(str, kw["mood_keywords"])),
                )
            # Moods are soft preferences: TMDB's "|" (OR) widens discovery to
            # any matching mood keyword instead of requiring all of them. TMDB
            # cannot group mixed AND/OR expressions, so no "," is used here.
            if keyword_groups:
                p["with_keywords"] = "|".join(str(kid) for group in keyword_groups for kid in group)
        response = await self._get("/discover/movie", p)
        if runtime_ceiling is None:
            return response

        max_runtime = kw["runtime_max"]
        results = []
        for movie in response.get("results", []):
            runtime = movie.get("runtime")
            if isinstance(runtime, int) and max_runtime < runtime <= runtime_ceiling:
                results.append(
                    {
                        **movie,
                        "is_overflow": True,
                        "overflow_minutes": runtime - max_runtime,
                    }
                )
            else:
                results.append(movie)
        return {**response, "results": results}
