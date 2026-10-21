import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.tools import MovieTools
from app.services import http_client
from app.services.tmdb_client import TMDBClient


@pytest.mark.asyncio
async def test_real_tmdb_latency_breakdown():
    tools = MovieTools()

    started = time.perf_counter()

    try:
        search_result = await tools.search_movies("Inception")
    except Exception:
        print("TMDB request timed out or returned empty. Verify TMDB_API_KEY and network connection.")
        pytest.skip("TMDB live API unreachable or unconfigured")

    search_elapsed = time.perf_counter() - started

    candidates = search_result.get("results") if isinstance(search_result, dict) else None
    if not candidates:
        print("TMDB request timed out or returned empty. Verify TMDB_API_KEY and network connection.")
        pytest.skip("TMDB live API unreachable or unconfigured")

    movie_id = candidates[0]["id"]

    started = time.perf_counter()

    details = await tools.get_movie_details(movie_id)

    details_elapsed = time.perf_counter() - started

    total = search_elapsed + details_elapsed

    print(f"\nTMDB search:   {search_elapsed:.3f}s")
    print(f"TMDB details:  {details_elapsed:.3f}s")
    print(f"Total:         {total:.3f}s")

    assert details["title"]
    assert details["runtime"] is not None


@pytest.mark.asyncio
async def test_real_agent_latency_breakdown():
    from app.agent.agent import MovieAgent

    agent = MovieAgent()
    started = time.perf_counter()
    try:
        response = await agent.run("Who directed Inception?")
    except Exception as exc:
        response = f"{type(exc).__name__}: {exc}"
    total = time.perf_counter() - started

    print(f"\nAgent total: {total:.3f}s")
    last_trace = getattr(agent, "last_trace", None)
    if last_trace:
        print(f"Trace info: {last_trace}")
    print(f"Agent response: {str(response)[:120]}...")


@pytest.mark.asyncio
async def test_mocked_end_to_end_latency(monkeypatch):
    from starlette.requests import Request as StarletteRequest

    from app import main
    from app.config import settings
    from app.services.wikidata_client import WikidataClient

    movie_id = 27205

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    async def fake_get(url, **_kwargs):
        await asyncio.sleep(0.04)
        if url.endswith("/search/movie"):
            return FakeResponse(
                {
                    "results": [
                        {
                            "id": movie_id,
                            "title": "Inception",
                            "original_title": "Inception",
                            "release_date": "2010-07-16",
                            "vote_average": 8.4,
                            "vote_count": 30000,
                            "popularity": 100,
                        }
                    ]
                }
            )
        return FakeResponse(
            {
                "id": movie_id,
                "title": "Inception",
                "runtime": 148,
                "release_date": "2010-07-16",
                "genres": [{"id": 878, "name": "Science Fiction"}],
                "credits": {"cast": [], "crew": []},
                "keywords": {"keywords": []},
            }
        )

    async def fake_post(_url, **_kwargs):
        await asyncio.sleep(0.04)
        return FakeResponse({"results": {"bindings": []}})

    shared_client = SimpleNamespace(
        get=AsyncMock(side_effect=fake_get),
        post=AsyncMock(side_effect=fake_post),
        is_closed=False,
    )
    monkeypatch.setattr(settings, "tmdb_api_key", "benchmark-test-key")
    monkeypatch.setattr(http_client, "get_shared_client", lambda: shared_client)
    TMDBClient._cache.clear()

    tools = MovieTools()

    async def benchmark_agent_stream(_message, _user_id, _conversation_id):
        search = await tools.search_movies("Inception")
        selected = search["results"][0]
        details = await tools.get_movie_details(selected["id"])
        await WikidataClient().query_sparql("SELECT * WHERE {}")
        yield f"{details['title']} runs for {details['runtime']} minutes."

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    class ConnectedRequest(StarletteRequest):
        async def is_disconnected(self):
            return False

    request = ConnectedRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/chat/stream",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )
    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    monkeypatch.setattr(main.agent, "stream", benchmark_agent_stream)

    started = time.perf_counter()
    response = await main.chat_stream(
        main.ChatIn(message="Who directed Inception?", conversation_id=42),
        SimpleNamespace(id=7),
        request,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    elapsed = time.perf_counter() - started
    simulated_upstream_elapsed = 0.04 * (shared_client.get.await_count + shared_client.post.await_count)
    internal_overhead = elapsed - simulated_upstream_elapsed

    assert any("Inception runs for 148 minutes." in chunk for chunk in chunks)
    assert shared_client.get.await_count == 2
    assert shared_client.post.await_count == 1
    print(f"Mocked end-to-end: {elapsed:.3f}s")
    print(f"Simulated upstream latency: {simulated_upstream_elapsed:.3f}s")
    print(f"Internal pipeline overhead: {internal_overhead:.3f}s")
    assert internal_overhead < 0.3, (
        f"Internal pipeline overhead was {internal_overhead:.3f}s " f"(end-to-end {elapsed:.3f}s)"
    )
