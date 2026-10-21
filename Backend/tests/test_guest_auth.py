import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.database import Base


@pytest.fixture
def isolated_auth_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(
        main.settings,
        "jwt_secret",
        "unit-test-jwt-secret-must-be-at-least-32-characters",
    )
    yield
    engine.dispose()


@pytest.mark.asyncio
async def test_cors_preflight_allows_local_frontend_origins():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://testserver") as client:
        response = await client.options(
            "/api/v1/chat/stream",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "PATCH" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "http://localhost:80",
        "http://127.0.0.1",
        "http://127.0.0.1:80",
    ],
)
async def test_cors_preflight_allows_docker_port_80_origins(origin):
    async with AsyncClient(
        transport=ASGITransport(app=main.app),
        base_url="http://testserver",
    ) as client:
        response = await client.options(
            "/api/v1/chat/stream",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_guest_chat_stream_uses_ephemeral_session_and_nullable_user_id(
    monkeypatch,
):
    cached = {}

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    async def cache_set(key, value, _max_entries, ttl=None):
        cached[key] = value

    async def cache_get(key, _ttl):
        return cached.get(key)

    async def guest_stream(
        message, user_id, conversation_id, guest_history, guest_session_id=None, **_kwargs
    ):
        assert user_id is None
        if message == "Recommend a thoughtful film.":
            assert uuid.UUID(conversation_id)
            assert guest_history == []
            yield "Try a quiet, thoughtful drama."
        else:
            assert message == "I wanna watch it tomorrow."
            assert len(guest_history) == 2
            assert guest_history[0] == {
                "role": "user",
                "content": "Recommend a thoughtful film.",
            }
            assert guest_history[1] == {
                "role": "assistant",
                "content": "Try a quiet, thoughtful drama.",
            }
            yield "I’ll keep that in mind."

    async def guest_run(message, user_id, conversation_id, guest_history, guest_session_id=None, **_kwargs):
        assert message == "Suggest another film."
        assert user_id is None
        assert uuid.UUID(conversation_id)
        assert guest_history == []
        return "Try a warm, character-driven comedy."

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    monkeypatch.setattr(main.shared_state, "cache_set", cache_set)
    monkeypatch.setattr(main.shared_state, "cache_get", cache_get)
    monkeypatch.setattr(main.agent, "stream", guest_stream)
    monkeypatch.setattr(main.agent, "run", guest_run)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://testserver") as client:
        guest_session = await client.post("/api/v1/guest/session")
        guest_session_id = guest_session.json()["guest_session_id"]
        response = await client.post("/api/v1/chat/stream", json={"message": "Recommend a thoughtful film."})
        conversation_id = json.loads(response.text.splitlines()[1][6:])["conversation_id"]
        assert client.cookies.get("guest_session_id") == guest_session_id
        continued = await client.post(
            "/api/v1/chat/stream",
            json={
                "message": "I wanna watch it tomorrow.",
                "conversation_id": conversation_id,
            },
        )
        regular_response = await client.post("/api/v1/chat", json={"message": "Suggest another film."})

    assert response.status_code == 200
    assert "guest_session_id=" in guest_session.headers["set-cookie"]
    assert "Try a quiet, thoughtful drama." in response.text
    assert regular_response.status_code == 200
    assert regular_response.json()["response"] == "Try a warm, character-driven comedy."
    assert uuid.UUID(regular_response.json()["conversation_id"])
    assert continued.status_code == 200
    assert "I’ll keep that in mind." in continued.text
    assert cached[f"guest-conversation:{guest_session_id}:{conversation_id}"] == [
        {"role": "user", "content": "Recommend a thoughtful film."},
        {"role": "assistant", "content": "Try a quiet, thoughtful drama."},
        {"role": "user", "content": "I wanna watch it tomorrow."},
        {"role": "assistant", "content": "I’ll keep that in mind."},
    ]
