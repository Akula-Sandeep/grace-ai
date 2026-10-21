"""Search finds conversations by what was said in them, not only by their titles (C3)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import Base, Conversation, Message, User


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    with sessions() as db:
        me = User(username="searcher", password_hash="x")
        other = User(username="someone-else", password_hash="x")
        db.add_all([me, other])
        db.flush()
        titled = Conversation(user_id=me.id, title="Arrival questions")
        talked = Conversation(user_id=me.id, title="Friday night")
        quiet = Conversation(user_id=me.id, title="Comedies")
        foreign = Conversation(user_id=other.id, title="Not mine")
        db.add_all([titled, talked, quiet, foreign])
        db.flush()
        long_answer = (
            "Here are some ideas for tonight. " * 6
            + "**Polytechnique** (2009) is Denis Villeneuve's shortest film at 77 minutes [1]. "
            + "It is stark and black-and-white. " * 6
        )
        db.add_all(
            [
                Message(conversation_id=titled.id, user_id=me.id, role="user", content="Explain the ending"),
                Message(
                    conversation_id=talked.id, user_id=me.id, role="user", content="Shortest Villeneuve film?"
                ),
                Message(conversation_id=talked.id, user_id=me.id, role="assistant", content=long_answer),
                Message(
                    conversation_id=quiet.id, user_id=me.id, role="user", content="100% fun, nothing_dark"
                ),
                Message(
                    conversation_id=quiet.id,
                    user_id=me.id,
                    role="assistant",
                    content="A deleted Polytechnique mention",
                    is_deleted=True,
                ),
                Message(conversation_id=foreign.id, user_id=other.id, role="user", content="Polytechnique"),
            ]
        )
        db.commit()
        yield SimpleNamespace(
            me=SimpleNamespace(id=me.id), titled=titled.id, talked=talked.id, quiet=quiet.id
        )
    engine.dispose()


def titles(page):
    return [item["title"] for item in page["items"]]


def test_finds_a_conversation_by_its_messages_with_an_excerpt(library):
    page = main.conversations(library.me, q="polytechnique")
    assert titles(page) == ["Friday night"]
    snippet = page["items"][0]["snippet"]
    text = snippet["text"]
    assert text[snippet["match_start"] : snippet["match_end"]] == "Polytechnique"
    assert "**" not in text and "[1]" not in text
    assert len(text) <= 150 and text.startswith("…") and text.endswith("…")
    assert snippet["role"] == "assistant"


def test_title_matches_need_no_excerpt(library):
    page = main.conversations(library.me, q="arrival")
    assert titles(page) == ["Arrival questions"]
    assert "snippet" not in page["items"][0]


def test_deleted_messages_and_other_users_are_never_searched(library):
    page = main.conversations(library.me, q="Polytechnique")
    assert "Comedies" not in titles(page) and "Not mine" not in titles(page)


def test_wildcards_in_the_query_are_literal(library):
    assert titles(main.conversations(library.me, q="100%")) == ["Comedies"]
    assert titles(main.conversations(library.me, q="fun_ nothing")) == []  # "_" would match the comma
    assert titles(main.conversations(library.me, q="nothing_dark")) == ["Comedies"]
    assert titles(main.conversations(library.me, q="%")) == ["Comedies"]


def test_excerpt_of_a_short_message_has_no_ellipses():
    snippet = main.search_snippet("Shortest Villeneuve film?", "villeneuve")
    assert snippet == {"text": "Shortest Villeneuve film?", "match_start": 9, "match_end": 19}
    assert main.search_snippet("nothing here", "dune") is None


def test_search_on_postgres_escapes_and_finds_message_text():
    import os
    import uuid

    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        pytest.skip("needs the Postgres test database")
    from app.database import SessionLocal

    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(username=f"pg-search-{tag}", password_hash="x")
        db.add(user)
        db.flush()
        convo = Conversation(user_id=user.id, title="Weekend")
        db.add(convo)
        db.flush()
        db.add(
            Message(
                conversation_id=convo.id,
                user_id=user.id,
                role="assistant",
                content="Try **Zodiac** at 100% focus",
            )
        )
        db.commit()
        me = SimpleNamespace(id=user.id)
    page = main.conversations(me, q="zodiac")
    assert [item["title"] for item in page["items"]] == ["Weekend"]
    assert page["items"][0]["snippet"]["text"] == "Try Zodiac at 100% focus"
    assert main.conversations(me, q="100%")["items"]
    assert not main.conversations(me, q="Zodiac_at")["items"]
