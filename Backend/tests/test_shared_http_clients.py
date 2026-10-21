import asyncio
from typing import ClassVar

import httpx
import pytest

from app.config import settings
from app.embeddings import EmbeddingDimensionError, EmbeddingService
from app.memory import MemoryService
from app.rag import RAGService
from app.services import http_client, wikidata_client
from app.services.movie_data_fallback import MovieDataFallbackEngine
from app.services.tmdb_client import TMDBClient
from app.services.wikidata_client import WikidataClient


def test_embedding_service_rejects_dimension_mismatches(monkeypatch):
    class WrongDimensionModel:
        def encode(self, _text, *, normalize_embeddings):
            assert normalize_embeddings is True
            return [0.0] * 383

    monkeypatch.setattr(
        EmbeddingService,
        "model",
        classmethod(lambda _cls: WrongDimensionModel()),
    )

    with pytest.raises(ValueError, match="expected 384, received 383"):
        EmbeddingService.encode("Interstellar")


def test_memory_and_rag_propagate_embedding_dimension_errors(monkeypatch):
    def mismatch(_cls, _text):
        raise EmbeddingDimensionError("test dimension mismatch")

    monkeypatch.setattr(EmbeddingService, "encode", classmethod(mismatch))

    with pytest.raises(EmbeddingDimensionError):
        MemoryService(None)._embedding("Interstellar", load=True)
    with pytest.raises(EmbeddingDimensionError):
        RAGService(None)._embed("Interstellar")
    with pytest.raises(EmbeddingDimensionError):
        MemoryService._cosine([1.0] * 384, [1.0] * 768)


def test_missing_embedding_dependency_is_an_explicit_fallback(monkeypatch):
    import builtins

    from app import memory, rag

    events = []
    original_import = builtins.__import__

    def import_without_sentence_transformers(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ModuleNotFoundError("sentence-transformers is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sentence_transformers)
    monkeypatch.setattr(memory, "log_event", lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(rag, "log_event", lambda *args, **kwargs: events.append((args, kwargs)))

    assert MemoryService(None)._embedding("Interstellar", load=True) is None
    assert RAGService(None)._embed("Interstellar") is None
    assert [event[0][0] for event in events] == [
        "memory_embedding_fallback",
        "rag_embedding_fallback",
    ]


def test_malformed_movie_reference_is_reported(monkeypatch):
    from app import memory

    events = []
    monkeypatch.setattr(
        memory,
        "log_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    MemoryService(None).save_movie_references(
        1,
        "conversation-id",
        "message-id",
        [{"tmdb_id": "not-a-number", "title": "Invalid"}],
    )

    assert events[0][0][0] == "movie_reference_fallback"
    assert events[0][1]["details"]["reason"] == "invalid_tmdb_id"


def test_wikidata_timeout_default_reflects_the_2026_10_09_live_evidence():
    """Regression guard for the data-driven calibration: an earlier 8.0s
    default meant a query that genuinely needed just over 8s to complete
    paid for a full extra retry cycle (observed: Wikidata passing, but
    only after "multiple timeout/retry events" totalling 16.8s, on an
    otherwise-clean VPN-off/Cloudflare-DNS run). 10.0s is not an arbitrary
    bump -- reverting it silently would reintroduce that latency tax
    without any test catching it."""
    assert settings.wikidata_timeout_seconds == 10.0


def test_shared_http_client_has_configured_pool_and_timeout():
    client = http_client.get_shared_client()
    assert client.timeout.read == 3.0
    assert client._transport._pool._max_connections == 100
    assert client._transport._pool._max_keepalive_connections == 50
    assert client._transport._pool._http2 is True


def test_provider_clients_share_the_lifespan_owned_http_pool(monkeypatch):
    shared = object()
    monkeypatch.setattr(http_client, "get_shared_client", lambda: shared)

    assert MovieDataFallbackEngine().client is shared


@pytest.mark.asyncio
async def test_shared_http_client_initializes_and_closes_with_event_loop():
    client = await http_client.initialize_shared_client()

    assert http_client.get_shared_client() is client
    assert client._transport._pool._http2 is True

    await http_client.close_shared_client()

    assert client.is_closed


@pytest.mark.asyncio
async def test_tmdb_uses_configured_deadline_and_raises_typed_timeout(monkeypatch):
    from app.services.upstream_errors import UpstreamTimeout

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 0.02)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    class SlowClient:
        """Every attempt times out, as a real httpx client would once the
        connection genuinely exceeds the per-request timeout it was given."""

        calls = 0

        async def get(self, *_args, **_kwargs):
            SlowClient.calls += 1
            request_timeout = _kwargs.get("timeout")
            assert isinstance(request_timeout, httpx.Timeout)
            assert request_timeout.read == 0.02
            raise httpx.ReadTimeout("simulated slow TMDB response")

    client = TMDBClient(client=SlowClient())
    with pytest.raises(UpstreamTimeout):
        await client._get("/movie/12")
    # All three retries were attempted within the overall deadline, not cut
    # short by it.
    assert SlowClient.calls == 3


@pytest.mark.asyncio
async def test_tmdb_outer_deadline_still_bounds_a_per_request_timeout_that_never_fires(monkeypatch):
    """A per-request timeout a fake client simply ignores must not hang
    forever: the overall wait_for ceiling is still a real upper bound."""
    from app.services.upstream_errors import UpstreamTimeout

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 0.05)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    class HangingClient:
        async def get(self, *_args, **_kwargs):
            await asyncio.sleep(10)

    client = TMDBClient(client=HangingClient())
    with pytest.raises(UpstreamTimeout):
        await client._get("/movie/12")


@pytest.mark.asyncio
async def test_tmdb_movie_details_uses_the_longer_details_timeout(monkeypatch):
    """get_movie_details() bundles nine sub-resources into one response via
    append_to_response -- a structurally much larger payload than any
    other TMDB call this client makes, and the one observed burning all 3
    retries' worth of the default tmdb_timeout_seconds under real network
    conditions. It must use tmdb_details_timeout_seconds, not the shared
    default, and that must actually reach the per-request httpx call."""
    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 6.0)
    monkeypatch.setattr(settings, "tmdb_details_timeout_seconds", 12.0)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    seen_timeouts = []

    class RecordingClient:
        async def get(self, *_args, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))

            class _Response:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"id": 42, "title": "Example"}

            return _Response()

    client = TMDBClient(client=RecordingClient())
    data = await client.get_movie_details(42)

    assert data == {"id": 42, "title": "Example"}
    assert len(seen_timeouts) == 1
    assert isinstance(seen_timeouts[0], httpx.Timeout)
    assert seen_timeouts[0].read == 12.0  # not the plain 6.0s default


@pytest.mark.asyncio
async def test_tmdb_other_calls_still_use_the_plain_default_timeout(monkeypatch):
    """A lighter call (unlike get_movie_details) must keep using the plain
    tmdb_timeout_seconds default -- the longer budget is specific to the
    one heavy, multi-resource call, not a blanket increase."""
    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 6.0)
    monkeypatch.setattr(settings, "tmdb_details_timeout_seconds", 12.0)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    seen_timeouts = []

    class RecordingClient:
        async def get(self, *_args, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))

            class _Response:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"results": []}

            return _Response()

    client = TMDBClient(client=RecordingClient())
    await client.get_similar_movies(42)

    assert len(seen_timeouts) == 1
    assert isinstance(seen_timeouts[0], httpx.Timeout)
    assert seen_timeouts[0].read == 6.0


@pytest.mark.asyncio
async def test_tmdb_defaults_to_its_own_dedicated_async_client(monkeypatch):
    """TMDB no longer falls back to app.services.http_client's shared
    client (used by Wikidata/MusicBrainz, http2=True) -- it has its own
    loop-scoped client, pinned to http2=False, specifically because the
    shared client's HTTP/2 negotiation was implicated in TMDB-only
    timeouts on some network paths (see tmdb_client.py's module comment).
    Monkeypatching http_client.get_shared_client must have NO effect on
    what TMDBClient() uses by default -- the dedicated client factory is
    what must be consulted instead."""
    from app.services import tmdb_client as tmdb_client_module

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    TMDBClient._cache.clear()

    class Response:
        status_code = 200
        headers: ClassVar[dict] = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 12}

    class DedicatedClient:
        is_closed = False

        async def get(self, *_args, **_kwargs):
            return Response()

    class SharedClient:
        is_closed = False

        async def get(self, *_args, **_kwargs):
            raise AssertionError("TMDB must not use the generic shared client")

    monkeypatch.setattr(http_client, "get_shared_client", lambda: SharedClient())
    monkeypatch.setattr(tmdb_client_module, "get_tmdb_shared_client", lambda: DedicatedClient())
    result = await TMDBClient()._get("/movie/12")

    assert result == {"id": 12}


@pytest.mark.asyncio
async def test_tmdb_dedicated_client_is_pinned_to_http1_1_and_is_loop_scoped():
    """The dedicated client is pinned to HTTP/1.1 (http2=False), distinct
    from the shared client's http2=True, and is cached per running event
    loop like the shared client is -- calling it twice within the same
    loop returns the same pooled client instead of a fresh one each time."""
    from app.services import tmdb_client as tmdb_client_module

    client = tmdb_client_module.get_tmdb_shared_client()
    try:
        assert client._transport._pool._http2 is False
        assert tmdb_client_module.get_tmdb_shared_client() is client

        shared = http_client.get_shared_client()
        assert shared._transport._pool._http2 is True
        assert client is not shared
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_tmdb_connect_phase_failure_gets_one_fewer_attempt_than_read_phase(monkeypatch):
    """A connect-phase failure (TCP/TLS handshake never completed --
    httpx.ConnectTimeout/ConnectError) means this route cannot reach TMDB
    at all. A third identical attempt cannot succeed where two already
    failed the same way, so it must stop at 2 attempts instead of the
    usual 3 -- this is the fix for "spend 37 seconds retrying a route
    that is clearly unreachable": the route fails fast instead of paying
    the full per-attempt timeout three times over."""
    from app.services.upstream_errors import UpstreamTimeout

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 5.0)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    calls = 0

    class UnreachableClient:
        async def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ConnectTimeout("simulated dead route")

    client = TMDBClient(client=UnreachableClient())
    with pytest.raises(UpstreamTimeout):
        await client._get("/movie/12")

    assert calls == 2  # not 3 -- a read-phase (ReadTimeout) failure still gets 3


@pytest.mark.asyncio
async def test_tmdb_read_phase_failure_still_gets_the_full_three_attempts(monkeypatch):
    """A read-phase failure (the connection succeeded; no response came
    back in time) is a different, more plausibly transient failure than a
    dead route, and must keep the full 3-attempt budget -- the fast-fail
    above is specific to connect-phase failures, not a blanket cut."""
    from app.services.upstream_errors import UpstreamTimeout

    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 5.0)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    calls = 0

    class SlowButConnectedClient:
        async def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("simulated slow response, connection was fine")

    client = TMDBClient(client=SlowButConnectedClient())
    with pytest.raises(UpstreamTimeout):
        await client._get("/movie/12")

    assert calls == 3


@pytest.mark.asyncio
async def test_tmdb_request_uses_a_short_connect_timeout_separate_from_read(monkeypatch):
    """The per-request httpx.Timeout must bound the connect phase to
    tmdb_connect_timeout_seconds regardless of how long the configured
    per-attempt (read) timeout is -- a long read budget for a heavy call
    (e.g. get_movie_details' 12s) must not also mean a 12s wait just to
    find out the connection itself cannot be established."""
    monkeypatch.setattr(settings, "tmdb_api_key", "test-key")
    monkeypatch.setattr(settings, "tmdb_timeout_seconds", 20.0)
    monkeypatch.setattr(settings, "tmdb_connect_timeout_seconds", 5.0)
    TMDBClient._cache.clear()
    TMDBClient._locks.clear()

    seen = []

    class RecordingClient:
        async def get(self, *_args, **kwargs):
            seen.append(kwargs.get("timeout"))

            class _Response:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"id": 1}

            return _Response()

    client = TMDBClient(client=RecordingClient())
    await client._get("/movie/1")

    assert len(seen) == 1
    assert seen[0].connect == 5.0
    assert seen[0].read == 20.0


@pytest.mark.asyncio
async def test_tmdb_dedicated_client_has_no_proxy_by_default(monkeypatch):
    """settings.tmdb_proxy_url is empty by default, so the dedicated TMDB
    client must not be constructed with a ``proxy`` kwarg at all -- the
    owner must explicitly opt in, never a default or implicit third-party
    proxy. Checked against the actual httpx.AsyncClient constructor call
    rather than the resulting client's ``_mounts``, since those also
    reflect this environment's own HTTP_PROXY/HTTPS_PROXY variables and
    would make the assertion depend on where the test happens to run."""
    from app.services import tmdb_client as tmdb_client_module

    assert not settings.tmdb_proxy_url
    seen_kwargs = {}
    original_init = httpx.AsyncClient.__init__

    def recording_init(self, *args, **kwargs):
        seen_kwargs.update(kwargs)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording_init)
    client = tmdb_client_module._new_tmdb_client()
    await client.aclose()

    assert "proxy" not in seen_kwargs


@pytest.mark.asyncio
async def test_tmdb_dedicated_client_applies_an_owner_configured_proxy(monkeypatch):
    """When the owner sets settings.tmdb_proxy_url, the dedicated TMDB
    client must actually be constructed with it -- this is the "securely
    configured, owner-controlled outbound proxy if genuinely needed"
    option, off by default, scoped to TMDB only (no other provider's
    client reads this setting)."""
    from app.services import tmdb_client as tmdb_client_module

    monkeypatch.setattr(settings, "tmdb_proxy_url", "http://127.0.0.1:38080")
    seen_kwargs = {}
    original_init = httpx.AsyncClient.__init__

    def recording_init(self, *args, **kwargs):
        seen_kwargs.update(kwargs)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording_init)
    client = tmdb_client_module._new_tmdb_client()
    await client.aclose()

    assert seen_kwargs.get("proxy") == "http://127.0.0.1:38080"


@pytest.mark.asyncio
async def test_wikidata_uses_shared_client_and_sparql_post_deadline(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class SharedClient:
        is_closed = False

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            assert url == WikidataClient.SEARCH_URL
            return Response({"search": [{"id": "Q123", "label": "Inception"}]})

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"results": {"bindings": []}})

    shared_client = SharedClient()
    monkeypatch.setattr(http_client, "get_shared_client", lambda: shared_client)
    assert await WikidataClient().awards("Inception") == []
    assert [item[0] for item in calls] == [
        WikidataClient.SEARCH_URL,
        WikidataClient.SPARQL_URL,
    ]
    assert all(item[1]["headers"]["User-Agent"] == WikidataClient.USER_AGENT for item in calls)
    assert calls[1][1]["timeout"] == wikidata_client.TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_wikidata_search_timeout_returns_empty_awards(monkeypatch):
    monkeypatch.setattr("app.services.wikidata_client.TIMEOUT_SECONDS", 0.01)

    class SlowClient:
        is_closed = False

        async def get(self, *_args, **_kwargs):
            await asyncio.sleep(0.1)

    slow = SlowClient()
    monkeypatch.setattr(http_client, "get_shared_client", lambda: slow)
    assert await WikidataClient().awards("Inception") == []


@pytest.mark.asyncio
async def test_wikidata_sparql_timeout_returns_empty_awards(monkeypatch):
    monkeypatch.setattr("app.services.wikidata_client.TIMEOUT_SECONDS", 0.01)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"search": [{"id": "Q123", "label": "Inception"}]}

    class SlowSparqlClient:
        """Mirrors a real httpx client: a per-request timeout fires as
        httpx.ReadTimeout once the simulated response would take longer
        than the ``timeout`` it was given, rather than hanging regardless
        of it -- the outer asyncio.wait_for in _post_sparql_once now has a
        deliberate 1.0s buffer over this (see its docstring), so a fake
        that just slept past the OLD tightly-matched outer deadline no
        longer exercises a timeout at all post-fix."""

        is_closed = False

        async def get(self, url, **_kwargs):
            assert url == WikidataClient.SEARCH_URL
            return Response()

        async def post(self, _url, **_kwargs):
            raise httpx.ReadTimeout("simulated slow Wikidata SPARQL response")

    monkeypatch.setattr(http_client, "get_shared_client", lambda: SlowSparqlClient())
    assert await WikidataClient().awards("Inception") == []
