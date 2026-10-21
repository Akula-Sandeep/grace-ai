import sys
import types


class _CryptContext:
    def __init__(self, *args, **kwargs):
        pass

    def hash(self, value):
        return value

    def verify(self, value, encoded):
        return value == encoded


if "passlib" not in sys.modules:
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")
    context.CryptContext = _CryptContext
    passlib.context = context
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context

import json
import math
from types import SimpleNamespace

from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import (
    Base,
    Conversation,
    Message,
    Movie,
    Rating,
    User,
    WatchlistItem,
)


def test_conversation_pages_are_small_and_use_stable_cursors(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    try:
        with sessions() as db:
            user = User(username="phase1-user", password_hash="test")
            db.add(user)
            db.flush()
            db.add_all(
                [Conversation(user_id=user.id, title=f"Conversation {index:04d}") for index in range(1000)]
            )
            db.commit()
            user_id = user.id

        user = SimpleNamespace(id=user_id)
        pages = []
        cursor = None
        while True:
            page = main.conversations(user, cursor=cursor, limit=30)
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break

        items = [item for page in pages for item in page["items"]]
        assert len(items) == 1000
        assert len({item["id"] for item in items}) == 1000
        payload_sizes = sorted(len(json.dumps(jsonable_encoder(page)).encode()) for page in pages)
        p95_index = math.ceil(0.95 * len(payload_sizes)) - 1
        assert payload_sizes[p95_index] < 10_000
    finally:
        engine.dispose()


def test_conversation_details_are_metadata_only(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1-messages.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    try:
        with sessions() as db:
            user = User(username="message-user", password_hash="test")
            db.add(user)
            db.flush()
            conversation = Conversation(user_id=user.id, title="A conversation")
            db.add(conversation)
            db.flush()
            for index in range(35):
                db.add(
                    Message(
                        conversation_id=conversation.id,
                        user_id=user.id,
                        role="user",
                        content=f"Message {index}",
                    )
                )
            db.commit()
            user_id, conversation_id = user.id, conversation.id

        user = SimpleNamespace(id=user_id)
        metadata = main.conversation(conversation_id, user)
        first_page = main.conversation_messages_v1(conversation_id, u=user)
        older_page = main.conversation_messages_v1(conversation_id, cursor=first_page["next_cursor"], u=user)

        assert "messages" not in metadata
        assert metadata["title"] == "A conversation"
        assert len(first_page["items"]) == 30
        assert len(older_page["items"]) == 5
        assert first_page["items"][0]["content"] == "Message 5"
        assert older_page["items"][-1]["content"] == "Message 4"
    finally:
        engine.dispose()


def test_state_embeds_movie_metadata_and_rating_without_extra_calls(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1-state.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    try:
        with sessions() as db:
            user = User(username="state-user", password_hash="test")
            movie = Movie(
                tmdb_id=123,
                title="A film",
                metadata_json={
                    "poster_path": "/poster.jpg",
                    "release_date": "2024-02-03",
                    "vote_average": 7.5,
                },
            )
            db.add_all([user, movie])
            db.flush()
            db.add(WatchlistItem(user_id=user.id, movie_id=movie.id))
            db.add(Rating(user_id=user.id, movie_id=movie.id, rating=8))
            db.commit()
            user_id = user.id

        state = main.state(SimpleNamespace(id=user_id))
        item = state["watchlist"][0]
        assert item["tmdb_id"] == 123
        assert item["poster_path"] == "/poster.jpg"
        assert item["year"] == "2024"
        assert item["rating"] == 8
        assert state["ratings"][0]["tmdb_id"] == 123
    finally:
        engine.dispose()


def test_user_state_is_bounded_and_reports_collection_totals(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'bounded-state.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    try:
        with sessions() as db:
            user = User(username="bounded-state", password_hash="test")
            db.add(user)
            db.flush()
            movies = [
                Movie(
                    tmdb_id=10_000 + idx,
                    title=f"Movie {idx}",
                    metadata_json={"release_date": "2024-01-01"},
                )
                for idx in range(160)
            ]
            db.add_all(movies)
            db.flush()
            db.add_all([WatchlistItem(user_id=user.id, movie_id=m.id) for m in movies[:80]])
            db.add_all(
                [
                    Rating(user_id=user.id, movie_id=m.id, rating=(idx % 10) + 1)
                    for idx, m in enumerate(movies[80:140])
                ]
            )
            db.commit()
            user_id = user.id

        payload = main.state(SimpleNamespace(id=user_id), limit=25)

        assert len(payload["watchlist"]) == 25
        assert payload["watchlist_count"] == 80
        assert payload["watchlist_has_more"] is True
        assert payload["rating_count"] == 60
        assert len(json.dumps(jsonable_encoder(payload)).encode()) < 20_000
    finally:
        engine.dispose()
