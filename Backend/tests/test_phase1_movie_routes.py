from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.services.upstream_errors import (
    UpstreamNotFound,
    UpstreamTimeout,
    UpstreamUnavailable,
)


class EmptyDB:
    def close(self):
        pass


class MovieTools:
    async def movie_recommendations(self, _movie_id, _page):
        return {"results": [{"id": 1}]}

    async def similar_movies(self, _movie_id, _page):
        return {"results": [{"id": 2}]}


@pytest.fixture
def guest_movie_routes(monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", EmptyDB)
    monkeypatch.setattr(main, "_movie_tools", lambda *_args, **_kwargs: MovieTools())
    monkeypatch.setattr(
        main,
        "MemoryService",
        lambda *_args, **_kwargs: SimpleNamespace(
            profile=lambda _user_id: pytest.fail("Guest route attempted personalization.")
        ),
    )


@pytest.mark.asyncio
async def test_guests_can_get_recommendations_and_similar_movies(guest_movie_routes):
    recommendations = await main.movie_recommendations(42, 1, None)
    similar = await main.movie_similar(42, 1, None)

    assert recommendations == {"results": [{"id": 1}]}
    assert similar == {"results": [{"id": 2}]}


@pytest.mark.asyncio
async def test_guests_can_compare_visuals_without_user_id(guest_movie_routes, monkeypatch):
    async def compare(_tools, movie_a, movie_b, _focus):
        assert (movie_a, movie_b) == (42, 43)
        return {"analysis": "A comparison"}

    monkeypatch.setattr(main.agent, "compare_visuals", compare)

    result = await main.compare_visuals(42, 43, u=None)

    assert result == {"analysis": "A comparison"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_error", "status_code"),
    [
        (UpstreamNotFound("missing"), 404),
        (UpstreamTimeout("slow"), 504),
        (UpstreamUnavailable("down"), 502),
    ],
)
async def test_movie_details_maps_upstream_failures(
    upstream_error, status_code, guest_movie_routes, monkeypatch
):
    class FailingTools:
        async def get_movie_details(self, _movie_id):
            raise upstream_error

    monkeypatch.setattr(main, "_movie_tools", lambda *_args, **_kwargs: FailingTools())

    with pytest.raises(HTTPException) as raised:
        await main.movie_details(42, u=None)

    assert raised.value.status_code == status_code
    if status_code == 502:
        assert raised.value.headers["Retry-After"] == "5"


@pytest.mark.asyncio
async def test_movie_search_returns_empty_results_when_tmdb_times_out(guest_movie_routes, monkeypatch):
    async def allow_rate_limit(*_args, **_kwargs):
        return True

    class TimedOutTools:
        async def search_movies(self, _query):
            raise UpstreamTimeout("TMDB request timed out.")

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    monkeypatch.setattr(main, "_movie_tools", lambda *_args, **_kwargs: TimedOutTools())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/movies/search",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )

    result = await main.movie_search_v1(request, q="Arrival", u=None)

    assert result == {
        "query": "Arrival",
        "page": 1,
        "total_results": 0,
        "results": [],
    }


@pytest.mark.asyncio
async def test_browsing_and_search_have_their_own_generous_limits(monkeypatch):
    """Opening one film loads about ten pieces of data; search runs as the user types.
    A shared 10/minute bucket blocked ordinary browsing (owner, 2026-10-21)."""
    calls = []

    async def allow_and_record(key, limit, window=60):
        calls.append((key, limit, window))
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_and_record)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/movies/search",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )

    await main._enforce_expensive_rate_limit(request, None, "search")
    await main._enforce_expensive_rate_limit(request, None)
    await main._enforce_expensive_rate_limit(request, SimpleNamespace(id=7), "model")

    assert calls == [
        ("search:guest:127.0.0.1", 60, 60),
        ("search:guest:127.0.0.1:daily", 2000, 86400),
        ("browse:guest:127.0.0.1", 60, 60),
        ("browse:guest:127.0.0.1:daily", 1500, 86400),
        ("model:user:7", 10, 60),
    ]
