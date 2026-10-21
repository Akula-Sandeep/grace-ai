"""End-to-end regression for the signed-in chat turn.

Drives ``main.chat_stream`` through the *real* ``MovieAgent.stream`` and
``dispatch_and_stream`` with a real ORM session factory. Only the external
model provider and the semantic router are faked. Runs on SQLite by default
and on PostgreSQL when ``POSTGRES_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import main
from app.agent import agent as agent_module
from app.database import Base, Conversation, Message, User


def _request():
    class ConnectedRequest(Request):
        async def is_disconnected(self):
            return False

    return ConnectedRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )


async def _events(response):
    events = []
    async for chunk in response.body_iterator:
        content = chunk.decode() if isinstance(chunk, bytes) else chunk
        for block in content.strip().split("\n\n"):
            lines = [line for line in block.splitlines() if not line.startswith(":")]
            if not lines:
                continue
            event = next(line[7:] for line in lines if line.startswith("event: "))
            data = next(line[6:] for line in lines if line.startswith("data: "))
            events.append({"event": event, **json.loads(data)})
    return events


class RecordingProvider:
    provider_name = "fake"

    def __init__(self, model_name, replies, *, fail=False):
        self.model_name = model_name
        self.replies = replies
        self.fail = fail
        self.requests = []
        self.last_usage = {}

    async def stream_events(self, request):
        from app.inference.models import StreamEvent

        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable")
        reply = self.replies.pop(0)
        for index in range(0, len(reply), 7):
            yield StreamEvent("text", text=reply[index : index + 7])
        yield StreamEvent("finish", finish_reason="stop")


@pytest.fixture
def sessions(monkeypatch):
    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(agent_module, "SessionLocal", factory)

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow)
    yield factory
    engine.dispose()


@pytest.fixture
def user_id(sessions):
    with sessions() as db:
        suffix = uuid.uuid4().hex[:10]
        user = User(
            username=f"e2e-{suffix}",
            email=f"e2e-{suffix}@example.test",
            email_verified=True,
            status="verified",
            password_hash="unused",
        )
        db.add(user)
        db.commit()
        return user.id


@pytest.mark.asyncio
async def test_signed_in_user_completes_three_turns_with_history(sessions, user_id, monkeypatch):
    replies = [
        "Interstellar is a 2014 science-fiction film.",
        "It was directed by Christopher Nolan.",
        "He also made Inception and Tenet.",
    ]
    fast = RecordingProvider("fast-model", list(replies))
    monkeypatch.setattr(main.agent, "router", SimpleNamespace(fast=lambda: fast, deep=lambda: fast))
    user = SimpleNamespace(id=user_id)

    conversation_id = None
    for prompt, expected in zip(
        (
            "Tell me about Interstellar",
            "Who directed it?",
            "What about his other sci-fi movies?",
        ),
        list(replies),
        strict=True,
    ):
        payload = main.ChatIn(
            message=prompt,
            conversation_id=conversation_id,
            client_message_id=uuid.uuid4(),
        )
        events = await _events(await main.chat_stream(payload, user, _request()))
        kinds = [event["event"] for event in events]
        assert kinds[0] == "started", kinds
        assert kinds[-1] == "done", events
        assert "error" not in kinds
        assert "".join(e["text"] for e in events if e["event"] == "token") == expected
        conversation_id = events[0]["conversation_id"]

    # The third request carried the earlier turns as conversation history.
    third_messages = fast.requests[-1].messages
    history_text = " ".join(
        str(m.get("content")) for m in third_messages if m["role"] in {"user", "assistant"}
    )
    assert "Tell me about Interstellar" in history_text
    assert "Christopher Nolan" in history_text

    with sessions() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None and conversation.user_id == user_id
        rows = db.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
        assert [row.role for row in rows] == ["user", "assistant"] * 3
        assert [row.status for row in rows if row.role == "assistant"] == ["complete"] * 3
        assert [row.content for row in rows if row.role == "assistant"] == replies


@pytest.mark.asyncio
async def test_non_streaming_chat_persists_both_messages(sessions, user_id, monkeypatch):
    fast = RecordingProvider("fast-model", ["A short answer."])
    monkeypatch.setattr(main.agent, "router", SimpleNamespace(fast=lambda: fast, deep=lambda: fast))
    result = await main.chat(
        main.ChatIn(message="Say something short"),
        SimpleNamespace(id=user_id),
        _request(),
    )
    assert result.response == "A short answer."
    with sessions() as db:
        rows = db.scalars(select(Message).where(Message.conversation_id == result.conversation_id)).all()
        assert sorted(row.role for row in rows) == ["assistant", "user"]


@pytest.mark.asyncio
async def test_provider_failure_yields_error_event_and_error_row(sessions, user_id, monkeypatch):
    failing = RecordingProvider("fast-model", [], fail=True)
    monkeypatch.setattr(
        main.agent,
        "router",
        SimpleNamespace(fast=lambda: failing, deep=lambda: failing),
    )

    async def no_gemini(*_args, **_kwargs):
        return None

    from app.services.movie_data_fallback import MovieDataFallbackEngine

    monkeypatch.setattr(
        MovieDataFallbackEngine,
        "grounded_search",
        classmethod(lambda cls, q: no_gemini()),
    )
    payload = main.ChatIn(message="What is the tallest mountain in Africa?", client_message_id=uuid.uuid4())
    events = await _events(await main.chat_stream(payload, SimpleNamespace(id=user_id), _request()))

    assert events[0]["event"] == "started"
    assert events[-1]["event"] == "error"
    assert "run_db" not in json.dumps(events)
    with sessions() as db:
        assistant = db.scalar(select(Message).where(Message.uuid_id == events[0]["message_id"]))
        assert assistant is not None and assistant.status == "error"


@pytest.mark.asyncio
async def test_small_talk_with_no_model_is_an_honest_outage_too(sessions, user_id, monkeypatch):
    """No canned chit-chat stands in for the assistant: an outage is shown as one, with retry."""
    failing = RecordingProvider("fast-model", [], fail=True)
    monkeypatch.setattr(main.agent, "router", SimpleNamespace(fast=lambda: failing, deep=lambda: failing))

    async def no_gemini(*_args, **_kwargs):
        return None

    from app.services.movie_data_fallback import MovieDataFallbackEngine

    monkeypatch.setattr(MovieDataFallbackEngine, "grounded_search", classmethod(lambda cls, q: no_gemini()))
    payload = main.ChatIn(message="Hello there, how are you today?", client_message_id=uuid.uuid4())
    events = await _events(await main.chat_stream(payload, SimpleNamespace(id=user_id), _request()))

    assert events[-1]["event"] == "error", events
    with sessions() as db:
        assistant = db.scalar(select(Message).where(Message.uuid_id == events[0]["message_id"]))
        assert assistant is not None and assistant.status == "error"
