from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main, security


@pytest.mark.asyncio
async def test_user_dependency_loads_database_user_off_event_loop(monkeypatch):
    calls = []

    async def fake_to_thread(fn):
        calls.append(fn)
        return (
            SimpleNamespace(
                id=7,
                is_active=True,
                status="verified",
                locked_until=None,
            )
            if len(calls) == 1
            else True
        )

    monkeypatch.setattr(security.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        security,
        "decode_access_token_claims",
        lambda token: {"sub": "7", "sid": "1"},
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", b"__Host-access_token=test-token")],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )

    user = await main.get_current_user_optional(request)

    assert user.id == 7
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_chat_does_not_requery_persisted_conversation(monkeypatch):
    async def allow(*args, **kwargs):
        return True

    async def run_agent(*args, **kwargs):
        return "answer"

    async def unexpected_database_round_trip(*args, **kwargs):
        raise AssertionError("chat should not re-read the conversation")

    monkeypatch.setattr(main.shared_state, "rate_limit", allow)
    monkeypatch.setattr(main.agent, "run", run_agent)
    monkeypatch.setattr(main, "run_sync", unexpected_database_round_trip)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/chat",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )

    result = await main.chat(
        main.ChatIn(message="hello", conversation_id=42),
        SimpleNamespace(id=7),
        request,
    )

    assert result.response == "answer"
    assert result.conversation_id == 42


def test_api_v1_health_route_exists(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        main,
        "SessionLocal",
        sessionmaker(bind=create_engine("sqlite://")),
    )
    client = TestClient(main.app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_negative_page_is_rejected_for_recommendations():
    client = TestClient(main.app)
    response = client.get("/api/v1/movies/1/recommendations?page=0")
    assert response.status_code == 422


def test_compare_visuals_static_route_precedes_dynamic_movie_routes(monkeypatch):
    async def fake_compare(movie_a, movie_b, focus, user):
        return {"movie_a": movie_a, "movie_b": movie_b}

    monkeypatch.setattr(main, "compare_visuals", fake_compare)
    response = TestClient(main.app).get("/api/v1/movies/compare/visuals?movie_a=1&movie_b=2")

    assert response.status_code == 200
    assert response.json() == {"movie_a": 1, "movie_b": 2}


def test_problem_details_echoes_valid_client_request_id():
    request_id = "client-request-123"
    response = TestClient(main.app).get(
        "/api/v1/does-not-exist",
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == request_id
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "urn:Grace:problem:not_found",
        "code": "not_found",
        "title": "Not found",
        "detail": "Not Found",
        "request_id": request_id,
    }


def test_production_rejects_sqlite_database(monkeypatch):
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "database_url", "sqlite:///./test.db")

    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        main._require_production_config()
