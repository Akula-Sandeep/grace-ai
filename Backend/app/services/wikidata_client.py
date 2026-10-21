import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import ClassVar

import httpx

from app.config import settings
from app.observability import log_event, logger
from app.services import http_client
from app.services.upstream_errors import (
    UpstreamTimeout,
    UpstreamUnavailable,
    classify_upstream_exception,
    describe_upstream_exception,
)

# The public query.wikidata.org SPARQL endpoint and the wbsearchentities REST
# calls routinely take several seconds under real network latency; this used
# to be hardcoded to 1.0s, which made every non-trivial query time out
# regardless of whether the requested data existed. It now tracks the
# configured setting so it can be tuned (and overridden in tests) without
# editing this module.
TIMEOUT_SECONDS = settings.wikidata_timeout_seconds
FAILURE_THRESHOLD = 3
RECOVERY_TIME_SECONDS = 60.0


class WikidataCircuitBreaker:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self):
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    def _transition(self, new_state: str) -> None:
        previous_state = self.state
        if previous_state == new_state:
            return
        self.state = new_state
        log_event(
            "circuit_breaker_transition",
            level=logging.WARNING,
            details={
                "service": "wikidata_sparql",
                "from_state": previous_state,
                "to_state": new_state,
                "consecutive_failures": self.consecutive_failures,
            },
        )

    async def allow_request(self) -> bool:
        async with self._lock:
            if self.state == self.CLOSED:
                return True
            if self.state == self.OPEN:
                if self.opened_at is None or time.monotonic() - self.opened_at < RECOVERY_TIME_SECONDS:
                    return False
                self._transition(self.HALF_OPEN)
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self.consecutive_failures = 0
            self.opened_at = None
            self._probe_in_flight = False
            self._transition(self.CLOSED)

    async def record_failure(self) -> None:
        async with self._lock:
            self.consecutive_failures += 1
            self._probe_in_flight = False
            if self.state == self.HALF_OPEN or self.consecutive_failures >= FAILURE_THRESHOLD:
                self._transition(self.OPEN)
                self.opened_at = time.monotonic()


wikidata_circuit_breaker = WikidataCircuitBreaker()


class WikidataClient:
    SEARCH_URL = "https://www.wikidata.org/w/api.php"
    SPARQL_URL = "https://query.wikidata.org/sparql"
    USER_AGENT = "Grace Movie Intelligence/5.0"

    # A successful SPARQL result, keyed by the exact query text. Shared
    # across every WikidataClient instance (the class is constructed fresh
    # per request, see movie_intelligence_client.py/person_intelligence.py),
    # so this is the only thing that makes a result outlive one request.
    #
    # Award/location facts for a given film are effectively static, so this
    # serves two purposes: (1) a fresh hit skips the network entirely for a
    # query seen recently, and (2) ONLY on a genuine failure (timeout
    # exhausted, or any other error) after a fresh attempt, a *stale* entry
    # is served instead of an empty list -- the last known-good answer is a
    # better degradation than silently reporting "no records" for data that
    # plainly exists (see query_sparql). A query that itself succeeds with
    # zero rows is cached too, because that is a confirmed-empty answer and
    # deserves the same memoization as any other successful result; it is
    # never confused with a failure-driven empty list, because failures
    # never write to this cache.
    _RESULT_CACHE: ClassVar[OrderedDict[str, tuple[float, list[dict]]]] = OrderedDict()

    # One lock per distinct query TEXT, shared across every WikidataClient
    # instance (same reasoning as _RESULT_CACHE above: a fresh instance is
    # constructed per request, so only a class-level structure can dedupe
    # across them). If several concurrent requests for the same film ask
    # for the exact same SPARQL query at the same time, only the first one
    # actually reaches the network; the rest wait on the same lock and then
    # read the now-populated cache instead of each issuing their own
    # duplicate POST to a public, rate-limited endpoint -- this is what
    # "reduces duplicate requests and excessive concurrent load" means in
    # practice here. Deliberately mirrors TMDBClient's identical
    # cache-key-lock pattern for the same reason: multiple callers racing
    # for the same not-yet-cached answer.
    _locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client or http_client.get_shared_client()

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @classmethod
    def _trim_locks(cls):
        if len(cls._locks) <= settings.cache_max_entries:
            return
        for key, lock in list(cls._locks.items()):
            if len(cls._locks) <= settings.cache_max_entries:
                break
            if not lock.locked():
                cls._locks.pop(key, None)

    async def _get_with_deadline(self, url, *, params, headers):
        try:
            return await asyncio.wait_for(
                self.client.get(url, params=params, headers=headers),
                timeout=TIMEOUT_SECONDS,
            )
        # httpx's own transport timeout (the shared client's 3.0s default,
        # see http_client.py) normally fires well before this wait_for's
        # TIMEOUT_SECONDS and raises httpx.TimeoutException, which is NOT a
        # TimeoutError subclass -- catching only TimeoutError would let that
        # (the common case) escape uncaught. See _post_sparql_once for the
        # related, previously-masked retry bug this same confusion caused.
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("wikidata_request_timed_out url=%s", url)
            return None

    async def _post_sparql_once(self, query: str) -> list[dict]:
        """One attempt: POST the query and return its result rows.

        Raises on any failure (timeout, transport, HTTP status, malformed
        body); never swallows to an empty list itself -- that decision
        belongs to the caller, which also owns the circuit breaker and the
        retry policy.

        The outer ``asyncio.wait_for`` and the inner httpx ``timeout=`` used
        to carry the *exact same* value. That is a race, not a double
        safety net: whichever fires first decides the exception TYPE, not
        just when it fires. httpx's own timeout raises ``httpx.ReadTimeout``
        (an ``httpx.TimeoutException``, which query_sparql's retry-on-timeout
        branch is written to catch); asyncio.wait_for's cancellation raises
        the builtin ``TimeoutError`` instead, which query_sparql treats as a
        non-retryable failure. With identical deadlines, asyncio.wait_for's
        call_later-scheduled cancellation reliably won that race in
        practice for the queries closest to the timeout boundary (the
        award-people query, which is slightly heavier than the anchored
        location/film-awards queries) -- so exactly the query that most
        needed the retry was the one silently never getting it, despite the
        retry logic itself being correct. Giving the outer wait_for a small
        buffer over the inner httpx timeout lets httpx's own timeout -- the
        one query_sparql is actually written to retry -- fire first in the
        normal case; the outer wait_for remains a defensive backstop for a
        hang httpx itself fails to bound (connection-pool exhaustion, etc.).
        """
        response = await asyncio.wait_for(
            self.client.post(
                self.SPARQL_URL,
                data={"query": query, "format": "json"},
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "application/sparql-results+json",
                },
                timeout=TIMEOUT_SECONDS,
            ),
            timeout=TIMEOUT_SECONDS + 1.0,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        rows = results.get("bindings") if isinstance(results, dict) else None
        if not isinstance(rows, list):
            raise TypeError("Wikidata returned malformed SPARQL results.")
        return rows

    @classmethod
    def _cache_get(cls, query: str, *, max_age: float | None) -> list[dict] | None:
        """Return a cached result, or ``None`` if there is none.

        ``max_age=None`` returns any cached entry regardless of age (the
        stale-fallback path); a numeric ``max_age`` only returns an entry
        fresher than that (the normal cache-hit path).
        """
        entry = cls._RESULT_CACHE.get(query)
        if entry is None:
            return None
        cached_at, rows = entry
        if max_age is not None and time.monotonic() - cached_at > max_age:
            return None
        cls._RESULT_CACHE.move_to_end(query)
        return rows

    @classmethod
    def _cache_put(cls, query: str, rows: list[dict]) -> None:
        cls._RESULT_CACHE[query] = (time.monotonic(), rows)
        cls._RESULT_CACHE.move_to_end(query)
        while len(cls._RESULT_CACHE) > settings.cache_max_entries:
            cls._RESULT_CACHE.popitem(last=False)

    # Status codes worth retrying once: 429 (rate limited -- the endpoint is
    # telling us to back off, not that anything is wrong with the query),
    # and 502/503/504 (classic transient upstream/gateway trouble on a
    # public service with no uptime guarantee). Any other 4xx (400 malformed
    # query, etc.) would just fail identically on a retry and is not here.
    _RETRYABLE_STATUS_CODES: ClassVar[frozenset[int]] = frozenset({429, 502, 503, 504})

    @staticmethod
    def _status_retry_delay(exc: httpx.HTTPStatusError, attempt: int) -> float:
        """How long to wait before retrying a 429/502/503/504.

        Prefers the server's own ``Retry-After`` header when present (this
        is what "respects Retry-After where provided" means in practice --
        a 429 in particular is the endpoint explicitly telling us how long
        to back off, and ignoring that in favor of our own guess is worse,
        not more polite). Capped at 5s regardless, since this is one bounded
        retry within an overall request budget, not a queue to wait in
        indefinitely. Falls back to a short exponential backoff when the
        header is absent or unparseable.
        """
        retry_after = exc.response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 5.0)
            except ValueError:
                pass
        return 0.5 * (2**attempt)

    async def _fail(
        self, query: str, exc: BaseException, *, raise_on_failure: bool, event: str
    ) -> list[dict]:
        """Shared failure path once retries (if any) are exhausted: record
        the breaker failure, serve a stale cached result if one exists
        (never confused with a confirmed-empty answer -- see query_sparql's
        docstring), and either raise a classified error or degrade to []."""
        await wikidata_circuit_breaker.record_failure()
        stale = self._cache_get(query, max_age=None)
        log_event(
            event,
            level=logging.WARNING,
            details={
                "state": wikidata_circuit_breaker.state,
                "error_type": type(exc).__name__,
                "category": classify_upstream_exception(exc),
                "detail": describe_upstream_exception(exc),
                "stale_fallback": stale is not None,
            },
        )
        if stale is not None:
            return stale
        if raise_on_failure:
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                raise UpstreamTimeout(
                    f"Wikidata SPARQL query timed out ({TIMEOUT_SECONDS}s budget): "
                    f"{describe_upstream_exception(exc)}"
                ) from exc
            raise UpstreamUnavailable(
                f"Wikidata SPARQL query failed ({classify_upstream_exception(exc)}): "
                f"{describe_upstream_exception(exc)}"
            ) from exc
        return []

    async def query_sparql(self, query: str, *, raise_on_failure: bool = False) -> list[dict]:
        """Run one SPARQL query through the shared, fail-open circuit breaker.

        A transient failure gets exactly one retry before giving up: a
        timeout (the public endpoint occasionally has a slow response), a
        429 (rate limited -- respects the server's own Retry-After), or a
        502/503/504 (gateway/upstream trouble). Any other failure kind (a
        bad query, a malformed body, a non-retryable 4xx) is not retried,
        since repeating it would just fail the same way. A retry still
        counts as a single logical attempt for the breaker: only the final
        outcome moves its failure count, so one retried query cannot trip
        it any faster than an un-retried one, and the overall per-query
        latency budget stays bounded regardless of which failure kind is
        being retried (this is the SAME retry ceiling as before -- it now
        just applies to more failure types, not more attempts).

        A fresh cached result (within ``wikidata_cache_ttl_seconds``) is
        returned without touching the network or the breaker at all -- award
        and location facts for a given film are effectively static, so
        there is no reason to re-query the same question repeatedly. Only a
        genuine success is ever written to the cache.

        If the request ultimately fails (retry exhausted, or any other
        error) and a *stale* cached result exists (seen on an earlier
        successful call, however long ago), that stale result is returned
        instead of an empty list, logged distinctly as a stale fallback --
        the last known-good answer is a better degradation than silently
        reporting "no records" for data that plainly exists. With no cached
        result at all, a failure still returns an empty list, as before.

        ``raise_on_failure`` defaults to False and every production caller
        relies on that: Wikidata failures must degrade a category
        gracefully, never surface as a 500 (see the Batch 11 note on why
        query_sparql's return-type contract stays this way). It exists so a
        verification tool (scripts/live_provider_smoke.py) can get a
        precise, honest pass/fail signal per query instead of inferring
        success from "rows happened to be non-empty".
        """
        cached = self._cache_get(query, max_age=settings.wikidata_cache_ttl_seconds)
        if cached is not None:
            logger.info("wikidata_sparql_cache_hit")
            return cached
        # Dedupe identical in-flight queries: if another caller for this
        # exact query text is already running (e.g. two concurrent requests
        # for the same film's awards), wait for it instead of issuing a
        # second, duplicate POST to a public, rate-limited endpoint. The
        # cache is re-checked immediately after acquiring the lock, since
        # the request we just waited on most likely just populated it.
        self._trim_locks()
        lock = self._locks.setdefault(query, asyncio.Lock())
        try:
            async with lock:
                cached = self._cache_get(query, max_age=settings.wikidata_cache_ttl_seconds)
                if cached is not None:
                    logger.info("wikidata_sparql_cache_hit")
                    return cached
                return await self._query_sparql_locked(query, raise_on_failure=raise_on_failure)
        finally:
            self._trim_locks()

    async def _query_sparql_locked(self, query: str, *, raise_on_failure: bool) -> list[dict]:
        """The actual network/retry/breaker path, run while holding this
        query's dedup lock (see query_sparql)."""
        if not await wikidata_circuit_breaker.allow_request():
            logger.info("wikidata_sparql_circuit_open")
            stale = self._cache_get(query, max_age=None)
            if stale is not None:
                return stale
            if raise_on_failure:
                raise UpstreamUnavailable("Wikidata SPARQL circuit breaker is open.")
            return []
        attempts = 2
        for attempt in range(attempts):
            try:
                rows = await self._post_sparql_once(query)
            # Both exception types are genuinely a timeout: httpx's own
            # per-request deadline raises httpx.TimeoutException, while the
            # outer asyncio.wait_for's defensive backstop (see
            # _post_sparql_once) raises the builtin TimeoutError. Treating
            # only one of them as retryable is exactly the bug that
            # silently defeated the retry for the award-people query --
            # whichever layer's clock happens to fire first must not change
            # whether a timeout gets its one retry.
            except (httpx.TimeoutException, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    logger.info(
                        "wikidata_sparql_retrying_after_timeout attempt=%s timeout_seconds=%s error_type=%s",
                        attempt + 1,
                        TIMEOUT_SECONDS,
                        type(exc).__name__,
                    )
                    continue
                # A timeout here is not evidence the requested data does not
                # exist -- it only means the query did not complete in time,
                # even after a retry. Callers that build "no X found"
                # messages from an empty result must not treat this the same
                # as a confirmed-empty answer.
                return await self._fail(
                    query, exc, raise_on_failure=raise_on_failure, event="wikidata_sparql_timed_out"
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in self._RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                    delay = self._status_retry_delay(exc, attempt)
                    logger.info(
                        "wikidata_sparql_retrying_after_status attempt=%s status=%s delay_seconds=%.2f",
                        attempt + 1,
                        status,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                return await self._fail(
                    query, exc, raise_on_failure=raise_on_failure, event="wikidata_sparql_failed"
                )
            except (
                httpx.RequestError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                return await self._fail(
                    query, exc, raise_on_failure=raise_on_failure, event="wikidata_sparql_failed"
                )
            else:
                await wikidata_circuit_breaker.record_success()
                self._cache_put(query, rows)
                return rows
        return []

    async def awards(self, title: str, year: int | None = None):
        headers = {"User-Agent": self.USER_AGENT, "Accept": "application/json"}
        response = await self._get_with_deadline(
            self.SEARCH_URL,
            params={
                "action": "wbsearchentities",
                "search": title,
                "language": "en",
                "format": "json",
                "limit": 5,
                "type": "item",
            },
            headers=headers,
        )
        if response is None:
            return []
        response.raise_for_status()
        candidates = response.json().get("search", [])
        candidate = next(
            (item for item in candidates if self._is_title_match(title, item.get("label", ""))),
            None,
        )
        if not candidate:
            return []
        qid = candidate["id"]
        query = """
        SELECT ?award ?awardLabel ?date ?recipientLabel WHERE {
          wd:{qid} p:P166|p:P1411 ?statement .
          ?statement ps:P166|ps:P1411 ?award .
          OPTIONAL { ?statement pq:P585 ?date . }
          OPTIONAL { ?statement pq:P54 ?recipient . }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
        } LIMIT 40
        """.replace("{qid}", qid)
        rows = await self.query_sparql(query)
        awards = []
        seen = set()
        for row in rows:
            name = (row.get("awardLabel") or {}).get("value")
            date = (row.get("date") or {}).get("value", "")[:4]
            recipient = (row.get("recipientLabel") or {}).get("value")
            key = (name, date, recipient)
            if name and key not in seen:
                seen.add(key)
                awards.append({"award": name, "year": date or None, "recipient": recipient})
        return awards

    @staticmethod
    def _is_title_match(expected, actual):
        expected_tokens = {token for token in expected.casefold().split() if len(token) > 2}
        actual_tokens = set(actual.casefold().split())
        return bool(expected_tokens) and len(expected_tokens & actual_tokens) >= max(
            1, min(2, len(expected_tokens))
        )
