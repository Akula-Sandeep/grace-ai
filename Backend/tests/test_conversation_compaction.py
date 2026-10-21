from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, Conversation, Message, User
from app.services.chat_orchestrator import assemble_chat_payload
from app.services.memory import maybe_compact_conversation


@pytest.fixture
def db_session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path}/conversation_memory.db")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


class _SummaryClient:
    def __init__(self):
        self.request = None
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="The user discussed Dune and prefers thoughtful science fiction."
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_conversation_compaction_updates_summary_after_threshold(db_session):
    user = User(username="compact-user", password_hash="test")
    db_session.add(user)
    db_session.commit()
    conversation = Conversation(user_id=user.id)
    db_session.add(conversation)
    db_session.commit()
    db_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"Turn message {index}",
            )
            for index in range(16)
        ]
    )
    db_session.commit()
    client = _SummaryClient()

    await maybe_compact_conversation(conversation.id, db_session, client)

    db_session.refresh(conversation)
    assert "prefers thoughtful science fiction" in conversation.summary
    assert client.request["model"] == settings.groq_summary_model == "openai/gpt-oss-20b"
    assert client.request["temperature"] == 0.2
    assert "Turn message 3" in client.request["messages"][1]["content"]
    assert "Turn message 4" not in client.request["messages"][1]["content"]
    assert conversation.summary_through_message_id is not None

    db_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"Turn message {index}",
            )
            for index in range(16, 20)
        ]
    )
    db_session.commit()
    await maybe_compact_conversation(conversation.id, db_session, client)
    incremental_messages = client.request["messages"][1]["content"]
    assert "Turn message 4" in incremental_messages
    assert "Turn message 7" in incremental_messages
    assert "Turn message 3" not in incremental_messages


def test_chat_payload_has_static_prefix_summary_and_bounded_history(db_session):
    user = User(username="payload-user", password_hash="test")
    db_session.add(user)
    db_session.commit()
    conversation = Conversation(user_id=user.id, summary="Discussed Dune.")
    db_session.add(conversation)
    db_session.commit()
    db_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"History {index}",
            )
            for index in range(12)
        ]
    )
    db_session.commit()

    payload = assemble_chat_payload(conversation.id, "I wanna watch it tomorrow", db_session)

    assert payload[0]["role"] == "system"
    assert "Understand the whole conversation before answering" in payload[0]["content"]
    assert payload[1] == {
        "role": "system",
        "content": "[Session Context Summary: Discussed Dune.]",
    }
    assert len(payload[2:-1]) == 7
    assert payload[-1] == {"role": "user", "content": "I wanna watch it tomorrow"}
    assert payload[2]["content"] == "History 5"


class _FakeTMDB:
    async def get_movie_details(self, movie_id):
        return {
            "id": movie_id,
            "title": "Interstellar",
            "release_date": "2014-11-07",
            "runtime": 169,
        }

    async def get_watch_providers(self, _movie_id, _region=None):
        return {
            "flatrate": [{"provider_name": "Netflix"}],
            "rent": [{"provider_name": "Prime Video"}],
            "buy": [],
        }


class _FakeMovieEngine:
    tmdb_client = _FakeTMDB()

    async def resolve_movie(self, title, *, year=None):
        assert title == "Interstellar"
        assert year is None
        return {
            "status": "found",
            "details": {
                "id": 157336,
                "title": "Interstellar",
                "release_date": "2014-11-07",
                "runtime": 169,
            },
        }

    async def close(self):
        return None
