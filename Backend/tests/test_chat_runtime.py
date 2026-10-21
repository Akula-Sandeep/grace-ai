import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app import main
from app.agent.agent import MovieAgent
from app.database import Base, Conversation, Message, User
from app.services.chat_orchestrator import assemble_chat_payload


def make_request(disconnect_after=None):
    class ConnectedRequest(Request):
        disconnect_checks = 0

        async def is_disconnected(self):
            self.disconnect_checks += 1
            return disconnect_after is not None and self.disconnect_checks > disconnect_after

    return ConnectedRequest(
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


async def read_events(response):
    events = []
    async for chunk in response.body_iterator:
        content = chunk.decode() if isinstance(chunk, bytes) else chunk
        for block in content.strip().split("\n\n"):
            lines = block.splitlines()
            event = next(line[7:] for line in lines if line.startswith("event: "))
            data = next(line[6:] for line in lines if line.startswith("data: "))
            events.append({"event": event, **json.loads(data)})
    return events


@pytest.mark.asyncio
async def test_movie_agent_run_filters_react_control_frames(monkeypatch):
    agent = MovieAgent.__new__(MovieAgent)

    async def stream(*_args, **_kwargs):
        yield 'event: status\ndata: {"stage":"retrieval"}\n\n'
        yield 'event: providers\ndata: {"providers":[]}\n\n'
        yield "The clean answer."

    monkeypatch.setattr(agent, "stream", stream)

    result = await agent.run("Find a movie", user_id=1)

    assert result == "The clean answer."


@pytest.fixture
def chat_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    with sessions() as db:
        user = User(id=17, username="runtime-user", password_hash="hash")
        db.add(user)
        db.commit()

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    yield sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_chat_turn_is_idempotent_and_persists_uuid_message_metadata(chat_db, monkeypatch):
    agent_calls = []

    async def fake_stream(message, user_id, conversation_id, **_kwargs):
        agent_calls.append((message, user_id, conversation_id))
        yield "A concise answer."

    monkeypatch.setattr(main.agent, "stream", fake_stream)
    client_message_id = uuid.uuid4()
    payload = main.ChatIn(
        message="Recommend a thoughtful movie",
        client_message_id=client_message_id,
    )

    first = await main.chat_stream(payload, SimpleNamespace(id=17), make_request())
    first_events = await read_events(first)
    conversation_id = first_events[0]["conversation_id"]
    second = await main.chat_stream(
        payload.model_copy(update={"conversation_id": conversation_id}),
        SimpleNamespace(id=17),
        make_request(),
    )
    second_events = await read_events(second)

    assert len(agent_calls) == 1
    assert second_events[0]["message_id"] == first_events[-1]["message_id"]
    assert second_events[-1]["event"] == "done"
    with chat_db() as db:
        messages = db.scalars(select(Message).where(Message.conversation_id == conversation_id)).all()
        user_messages = [message for message in messages if message.role == "user"]
        assistant = next(message for message in messages if message.role == "assistant")
        conversation = db.get(Conversation, conversation_id)
        assert len(user_messages) == 1
        assert user_messages[0].uuid_id != str(client_message_id)
        assert user_messages[0].client_message_id == str(client_message_id)
        assert assistant.parent_id == user_messages[0].uuid_id
        assert uuid.UUID(assistant.uuid_id)
        assert assistant.status == "complete"
        assert assistant.content == "A concise answer."
        assert assistant.model == main.settings.groq_deep_model
        assert assistant.latency_ms is not None
        assert conversation.title != "New movie conversation"
        assert 3 <= len(conversation.title.split()) <= 6


@pytest.mark.asyncio
async def test_same_client_message_id_is_scoped_to_each_user(chat_db, monkeypatch):
    with chat_db() as db:
        db.add(User(id=18, username="second-user", password_hash="hash"))
        db.commit()

    async def fake_stream(*_args, **_kwargs):
        yield "A concise answer."

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    monkeypatch.setattr(main.agent, "stream", fake_stream)
    client_message_id = uuid.uuid4()

    first = await main.chat_stream(
        main.ChatIn(message="Recommend a film", client_message_id=client_message_id),
        SimpleNamespace(id=17),
        make_request(),
    )
    second = await main.chat_stream(
        main.ChatIn(message="Recommend a film", client_message_id=client_message_id),
        SimpleNamespace(id=18),
        make_request(),
    )
    await read_events(first)
    await read_events(second)

    with chat_db() as db:
        rows = db.scalars(
            select(Message).where(
                Message.role == "user",
                Message.client_message_id == str(client_message_id),
            )
        ).all()
    assert {row.user_id for row in rows} == {17, 18}
    assert len({row.uuid_id for row in rows}) == 2


@pytest.mark.asyncio
async def test_title_generation_is_scheduled_only_for_the_first_turn(chat_db, monkeypatch):
    async def fake_stream(*_args, **_kwargs):
        yield "A concise answer."

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    monkeypatch.setattr(main.shared_state, "rate_limit", allow_rate_limit)
    monkeypatch.setattr(main.agent, "stream", fake_stream)

    first_tasks = BackgroundTasks()
    first = await main.chat_stream(
        main.ChatIn(message="What makes Arrival memorable?"),
        SimpleNamespace(id=17),
        make_request(),
        background_tasks=first_tasks,
    )
    first_events = await read_events(first)

    second_tasks = BackgroundTasks()
    second = await main.chat_stream(
        main.ChatIn(message="Tell me more", conversation_id=first_events[0]["conversation_id"]),
        SimpleNamespace(id=17),
        make_request(),
        background_tasks=second_tasks,
    )
    await read_events(second)

    assert sum(task.func is main._generate_conversation_title for task in first_tasks.tasks) == 1
    assert not any(task.func is main._generate_conversation_title for task in second_tasks.tasks)


@pytest.mark.asyncio
async def test_title_generation_does_not_call_llm_before_a_completed_reply(chat_db, monkeypatch):
    with chat_db() as db:
        conversation = Conversation(user_id=17, title="New movie conversation")
        db.add(conversation)
        db.commit()
        conversation_id = conversation.id

    calls = []

    class FastModel:
        async def generate(self, *_args, **_kwargs):
            calls.append("generate")
            return SimpleNamespace(text="A generated title")

    class Router:
        def fast(self):
            return FastModel()

    monkeypatch.setattr(main.agent, "router", Router())

    await main._generate_conversation_title(conversation_id, "A prompt", "Fallback title")

    assert calls == []


@pytest.mark.asyncio
async def test_stream_failure_stores_empty_error_message_and_excludes_it_from_history(chat_db, monkeypatch):
    async def failing_stream(*_args, **_kwargs):
        raise TimeoutError
        yield  # pragma: no cover

    monkeypatch.setattr(main.agent, "stream", failing_stream)
    response = await main.chat_stream(
        main.ChatIn(message="Explain this movie"),
        SimpleNamespace(id=17),
        make_request(),
    )
    events = await read_events(response)
    conversation_id = events[0]["conversation_id"]

    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "timeout"
    with chat_db() as db:
        assistant = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
        assert assistant.status == "error"
        assert assistant.content == ""
        assert assistant.error_code == "timeout"
        prompt = assemble_chat_payload(
            conversation_id,
            "A later question",
            db,
            current_message_id=None,
        )
        assert all(item["content"] != "" for item in prompt)
        assert not any(item.get("role") == "assistant" and item.get("content") == "" for item in prompt)


@pytest.mark.asyncio
async def test_stop_persists_partial_answer_and_continue_does_not_duplicate_user(chat_db, monkeypatch):
    async def fake_stream(_message, _user_id, _conversation_id, continue_from_message_id=None, **_kwargs):
        if continue_from_message_id is None:
            yield "Partial answer."
            await asyncio.sleep(0.3)
            yield "This part should not be persisted."
        else:
            yield "Continued answer."

    monkeypatch.setattr(main.agent, "stream", fake_stream)
    first_response = await main.chat_stream(
        main.ChatIn(message="Explain a movie", client_message_id=uuid.uuid4()),
        SimpleNamespace(id=17),
        make_request(disconnect_after=1),
    )
    first_events = await read_events(first_response)
    conversation_id = first_events[0]["conversation_id"]
    stopped_id = first_events[0]["message_id"]

    with chat_db() as db:
        stopped = db.scalar(select(Message).where(Message.uuid_id == stopped_id))
        assert stopped.status == "stopped"
        assert stopped.content == "Partial answer."

    continuation = await main.chat_stream(
        main.ChatIn(
            message="Continue",
            conversation_id=conversation_id,
            client_message_id=uuid.uuid4(),
            continue_from=uuid.UUID(stopped_id),
        ),
        SimpleNamespace(id=17),
        make_request(),
    )
    continuation_events = await read_events(continuation)
    assert continuation_events[-1]["event"] == "done"

    with chat_db() as db:
        messages = db.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
        users = [message for message in messages if message.role == "user"]
        answers = [message for message in messages if message.role == "assistant"]
        assert len(users) == 1
        assert len(answers) == 2
        assert answers[0].status == "stopped"
        assert answers[0].content == "Partial answer."
        assert answers[1].status == "complete"
        assert answers[1].content == "Continued answer."
        assert answers[1].parent_id == answers[0].uuid_id


def test_edit_creates_a_new_branch_and_history_uses_only_selected_lineage(chat_db):
    with chat_db() as db:
        conversation = Conversation(user_id=17, title="Branches")
        db.add(conversation)
        db.flush()
        original = Message(
            conversation_id=conversation.id,
            user_id=17,
            role="user",
            content="Original question",
        )
        db.add(original)
        db.flush()
        original_answer = Message(
            conversation_id=conversation.id,
            user_id=17,
            role="assistant",
            content="Original answer",
            parent_id=original.uuid_id,
        )
        db.add(original_answer)
        db.flush()
        other_user = Message(
            conversation_id=conversation.id,
            user_id=17,
            role="user",
            content="Different branch question",
            parent_id=original_answer.uuid_id,
        )
        db.add(other_user)
        db.commit()
        conversation_id = conversation.id
        edited_id = original.uuid_id

    edited = main.ChatIn(
        message="Edited question",
        conversation_id=conversation_id,
        client_message_id=uuid.uuid4(),
        edit_message_id=uuid.UUID(edited_id),
    )
    _conversation_id, edited_user, edited_assistant, replay = main._prepare_persisted_turn(17, edited)

    assert replay is False
    assert edited_user.content == "Edited question"
    assert edited_user.parent_id is None
    assert edited_assistant.parent_id == edited_user.uuid_id
    with chat_db() as db:
        payload = assemble_chat_payload(
            conversation_id,
            "A follow-up to the edited question",
            db,
            current_message_id=edited_user.id,
        )
    assert [item["content"] for item in payload if item["role"] != "system"] == [
        "A follow-up to the edited question"
    ]
