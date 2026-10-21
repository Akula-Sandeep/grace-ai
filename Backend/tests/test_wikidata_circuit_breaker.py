import time

import httpx
import pytest

from app.services import wikidata_client
from app.services.wikidata_client import WikidataCircuitBreaker, WikidataClient


@pytest.fixture
def breaker(monkeypatch):
    instance = WikidataCircuitBreaker()
    monkeypatch.setattr(wikidata_client, "wikidata_circuit_breaker", instance)
    return instance


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_sparql_post_uses_explicit_timeout_and_returns_bindings(breaker):
    rows = [{"awardLabel": {"value": "Example Prize"}}]
    client = FakeClient([FakeResponse({"results": {"bindings": rows}})])
    wikidata = WikidataClient(client=client)

    result = await wikidata.query_sparql("SELECT * WHERE {}")

    assert result == rows
    assert client.calls[0][0] == WikidataClient.SPARQL_URL
    assert client.calls[0][1]["timeout"] == wikidata_client.TIMEOUT_SECONDS
    assert client.calls[0][1]["data"]["query"] == "SELECT * WHERE {}"
    assert breaker.state == WikidataCircuitBreaker.CLOSED


@pytest.mark.asyncio
async def test_sparql_failures_open_circuit_and_bypass_requests(breaker):
    """Each logical query_sparql() call now retries once on timeout before
    giving up, so three logical failures take six physical HTTP attempts --
    but the breaker still only moves once per logical call, so three logical
    failures (not six) are what open it.
    """
    client = FakeClient([httpx.TimeoutException("upstream timeout")] * 6)
    wikidata = WikidataClient(client=client)

    assert await wikidata.query_sparql("query") == []
    assert await wikidata.query_sparql("query") == []
    assert await wikidata.query_sparql("query") == []
    assert breaker.state == WikidataCircuitBreaker.OPEN
    assert breaker.consecutive_failures == 3
    assert len(client.calls) == 6

    assert await wikidata.query_sparql("query") == []
    assert len(client.calls) == 6


@pytest.mark.asyncio
async def test_sparql_tolerates_realistic_query_latency(breaker):
    """A query slower than the old hardcoded 1.0s -- but still well within a
    realistic public-SPARQL-endpoint response time -- must complete and
    return real rows, not time out and come back empty. A timeout is not
    evidence that the requested data does not exist.
    """
    import asyncio

    rows = [{"placeLabel": {"value": "Reykjavik"}}]

    class SlowButRealClient:
        async def post(self, url, **kwargs):
            await asyncio.sleep(1.4)
            return FakeResponse({"results": {"bindings": rows}})

    wikidata = WikidataClient(client=SlowButRealClient())

    result = await wikidata.query_sparql("SELECT * WHERE {}")

    assert result == rows
    assert breaker.state == WikidataCircuitBreaker.CLOSED


@pytest.mark.asyncio
async def test_sparql_timeout_is_logged_with_a_classified_category(breaker, monkeypatch, caplog):
    """A genuine timeout must still be distinguishable from a confirmed-empty
    result in the logs, so it is never silently read as "no data exists".
    """
    import logging

    # The retry-once-on-timeout policy means a logical failure needs two
    # queued timeouts (the original attempt and its one retry).
    client = FakeClient([httpx.TimeoutException("upstream timeout")] * 2)
    wikidata = WikidataClient(client=client)

    with caplog.at_level(logging.WARNING, logger="movie_agent"):
        result = await wikidata.query_sparql("query")

    assert result == []
    assert len(client.calls) == 2
    assert any("wikidata_sparql_timed_out" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_sparql_http_error_counts_and_half_open_success_resets(breaker, monkeypatch):
    """A 503 is retryable (see _RETRYABLE_STATUS_CODES), so one *logical*
    failure now costs 2 network calls (the original attempt plus its one
    retry) before the breaker's failure count moves -- the breaker still
    counts logical outcomes, not raw network calls, same as for a timeout."""
    request = httpx.Request("POST", WikidataClient.SPARQL_URL)
    failure = httpx.HTTPStatusError(
        "server error",
        request=request,
        response=httpx.Response(503, request=request),
    )
    client = FakeClient(
        [
            failure,
            failure,  # logical failure 1 (original + retry)
            failure,
            failure,  # logical failure 2
            failure,
            failure,  # logical failure 3 -- trips the breaker
            FakeResponse({"results": {"bindings": []}}),
        ]
    )
    wikidata = WikidataClient(client=client)

    for _ in range(3):
        assert await wikidata.query_sparql("query") == []
    assert breaker.state == WikidataCircuitBreaker.OPEN
    assert len(client.calls) == 6

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        wikidata_client.time,
        "monotonic",
        lambda: real_monotonic() + wikidata_client.RECOVERY_TIME_SECONDS + 1,
    )
    assert await wikidata.query_sparql("query") == []
    assert breaker.state == WikidataCircuitBreaker.CLOSED
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_sparql_429_respects_retry_after_header(breaker):
    """A 429 must back off for however long Wikidata's own Retry-After
    header says, not a fixed guess -- the server is explicitly telling us
    the wait time, and a retry policy that ignores it is worse, not more
    considerate, of a public rate-limited endpoint."""
    request = httpx.Request("POST", WikidataClient.SPARQL_URL)
    rate_limited = httpx.HTTPStatusError(
        "rate limited",
        request=request,
        response=httpx.Response(429, request=request, headers={"retry-after": "0.05"}),
    )
    rows = [{"awardLabel": {"value": "Example Prize"}}]
    client = FakeClient([rate_limited, FakeResponse({"results": {"bindings": rows}})])
    wikidata = WikidataClient(client=client)

    started = time.monotonic()
    result = await wikidata.query_sparql("query")
    elapsed = time.monotonic() - started

    assert result == rows
    assert len(client.calls) == 2  # the retry actually happened
    assert breaker.state == WikidataCircuitBreaker.CLOSED
    assert elapsed >= 0.05  # actually waited the server-specified delay


def test_sparql_429_retry_delay_is_capped_even_with_a_huge_retry_after():
    """Retry-After is respected, but not unconditionally -- a (malicious or
    misconfigured) huge value must not stall a single query far beyond the
    overall request's latency budget."""
    request = httpx.Request("POST", WikidataClient.SPARQL_URL)
    rate_limited = httpx.HTTPStatusError(
        "rate limited",
        request=request,
        response=httpx.Response(429, request=request, headers={"retry-after": "600"}),
    )
    assert WikidataClient._status_retry_delay(rate_limited, 0) == 5.0


@pytest.mark.asyncio
async def test_sparql_502_without_retry_after_uses_bounded_backoff_and_retries(breaker):
    """A 502 (no Retry-After header, typical for a bare gateway error) must
    still get the same one bounded retry as a timeout or a 429 -- it is a
    classic transient upstream failure on a public service with no uptime
    guarantee, not a reason to give up immediately."""
    request = httpx.Request("POST", WikidataClient.SPARQL_URL)
    bad_gateway = httpx.HTTPStatusError(
        "bad gateway",
        request=request,
        response=httpx.Response(502, request=request),
    )
    rows = [{"placeLabel": {"value": "Reykjavik"}}]
    client = FakeClient([bad_gateway, FakeResponse({"results": {"bindings": rows}})])
    wikidata = WikidataClient(client=client)

    result = await wikidata.query_sparql("query")

    assert result == rows
    assert len(client.calls) == 2
    assert breaker.state == WikidataCircuitBreaker.CLOSED


@pytest.mark.asyncio
async def test_sparql_non_retryable_4xx_is_not_retried(breaker):
    """A plain 400 (malformed query) would fail identically on a retry, so
    unlike 429/502/503/504 it must NOT be retried -- retrying it only
    wastes latency budget for a failure a retry cannot fix."""
    request = httpx.Request("POST", WikidataClient.SPARQL_URL)
    bad_request = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(400, request=request),
    )
    client = FakeClient([bad_request])
    wikidata = WikidataClient(client=client)

    result = await wikidata.query_sparql("query")

    assert result == []
    assert len(client.calls) == 1  # no retry
    assert breaker.state == WikidataCircuitBreaker.CLOSED
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_sparql_success_is_cached_and_served_without_a_second_network_call(breaker):
    """Award/location facts for a given film are effectively static, so a
    second query_sparql() call for the exact same query text within the
    cache TTL must be served from the cache, not the network."""
    rows = [{"awardLabel": {"value": "Example Prize"}}]
    client = FakeClient([FakeResponse({"results": {"bindings": rows}})])
    wikidata = WikidataClient(client=client)

    first = await wikidata.query_sparql("SELECT * WHERE { ?award }")
    second = await wikidata.query_sparql("SELECT * WHERE { ?award }")

    assert first == rows
    assert second == rows
    assert len(client.calls) == 1  # the second call never touched the network


@pytest.mark.asyncio
async def test_sparql_failure_falls_back_to_the_last_known_good_result(breaker):
    """A later timeout (or any other failure) on a query that previously
    succeeded must serve that stale cached result rather than an empty
    list -- the whole point being that a timeout is never confirmation
    that the data does not exist, and here real data was already seen."""
    rows = [{"placeLabel": {"value": "Reykjavik"}}]
    client = FakeClient(
        [
            FakeResponse({"results": {"bindings": rows}}),
            httpx.TimeoutException("upstream timeout"),
            httpx.TimeoutException("upstream timeout"),
        ]
    )
    wikidata = WikidataClient(client=client)

    first = await wikidata.query_sparql("SELECT * WHERE { ?place }")
    assert first == rows

    # Force the next lookup to miss the fresh-cache window but still find
    # the stale entry, simulating the TTL having elapsed.
    real_monotonic = time.monotonic
    monkeypatch_target = real_monotonic() + wikidata_client.settings.wikidata_cache_ttl_seconds + 1
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wikidata_client.time, "monotonic", lambda: monkeypatch_target)
        second = await wikidata.query_sparql("SELECT * WHERE { ?place }")

    assert second == rows  # stale fallback, not an empty list
    assert len(client.calls) == 3  # the one success, plus the retried failure


@pytest.mark.asyncio
async def test_sparql_failure_with_no_prior_success_still_returns_empty(breaker):
    """With nothing cached at all, a failure still degrades to an empty
    list exactly as before -- the stale-fallback path must never invent
    data that was never actually seen."""
    client = FakeClient([httpx.TimeoutException("upstream timeout")] * 2)
    wikidata = WikidataClient(client=client)

    result = await wikidata.query_sparql("SELECT * WHERE { ?never_seen }")

    assert result == []


@pytest.mark.asyncio
async def test_sparql_genuinely_empty_success_is_cached_as_a_real_answer(breaker):
    """A query that succeeds with zero bindings is a confirmed-empty
    answer, not a failure, and must be cached and reused as such."""
    client = FakeClient(
        [
            FakeResponse({"results": {"bindings": []}}),
        ]
    )
    wikidata = WikidataClient(client=client)

    first = await wikidata.query_sparql("SELECT * WHERE { ?nothing }")
    second = await wikidata.query_sparql("SELECT * WHERE { ?nothing }")

    assert first == []
    assert second == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_sparql_dedupes_concurrent_identical_queries(breaker):
    """Two concurrent callers asking for the exact same query text must
    not both hit the network -- the second should wait for the first and
    then read its now-cached result, reducing duplicate load on a public,
    rate-limited endpoint. This mirrors TMDBClient's identical
    cache-key-lock pattern for the same reason."""
    import asyncio as _asyncio

    rows = [{"awardLabel": {"value": "Example Prize"}}]

    class SlowOnceClient:
        """Only one call is queued -- if dedup fails and a second request
        reaches the network, this raises IndexError instead of silently
        returning something plausible, so a dedup regression cannot pass
        by accident."""

        def __init__(self):
            self.calls = 0

        async def post(self, url, **kwargs):
            self.calls += 1
            await _asyncio.sleep(0.05)
            return FakeResponse({"results": {"bindings": rows}})

    client = SlowOnceClient()
    wikidata = WikidataClient(client=client)

    first, second = await _asyncio.gather(
        wikidata.query_sparql("SELECT * WHERE { ?award } # dedup-test"),
        wikidata.query_sparql("SELECT * WHERE { ?award } # dedup-test"),
    )

    assert first == rows
    assert second == rows
    assert client.calls == 1  # the second caller waited and reused the cache


@pytest.mark.asyncio
async def test_sparql_retries_a_bare_timeouterror_exactly_like_an_httpx_timeout(breaker):
    """Regression guard for the bug that silently defeated the retry for
    the award-people query in production: the outer asyncio.wait_for in
    _post_sparql_once raises the builtin TimeoutError (not an
    httpx.TimeoutException) when IT is the one that fires first. Before the
    fix, only httpx.TimeoutException was retried, so whichever layer's
    clock won an essentially-tied race silently decided whether a timeout
    got its one retry -- and the heavier award-people query was the one
    that kept losing it. A bare TimeoutError must get exactly the same
    one retry as an httpx.TimeoutException."""
    rows = [{"awardLabel": {"value": "Example Prize"}}]
    client = FakeClient(
        [TimeoutError("outer wait_for fired first"), FakeResponse({"results": {"bindings": rows}})]
    )
    wikidata = WikidataClient(client=client)

    result = await wikidata.query_sparql("SELECT * WHERE { ?award_people }")

    assert result == rows
    assert len(client.calls) == 2  # the retry actually happened
    assert breaker.state == WikidataCircuitBreaker.CLOSED
