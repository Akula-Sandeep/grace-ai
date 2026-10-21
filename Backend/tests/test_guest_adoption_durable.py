import sys
import types
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


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

from starlette.requests import Request
from starlette.responses import Response

from app import database
from app.database import Base, Conversation, GuestAdoption, Movie, User, WatchlistItem
from app.routers import auth as auth_router
from app.shared_state import shared_state


@pytest.mark.asyncio
async def test_guest_adoption_is_durable_and_idempotent_after_cache_is_cleared(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'guest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth_router, "SessionLocal", factory)

    guest_id = str(uuid.uuid4())
    user_id = None
    with factory() as db:
        user = User(username="guest-user", email="guest@example.test", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

    cache = {
        f"guest-watchlist:{guest_id}": [{"tmdb_id": 27205, "movie_title": "Inception"}],
        f"guest-watched:{guest_id}": [],
        f"guest-interactions:{guest_id}": {"conversation_id": "guest-1"},
        f"guest-conversation:{guest_id}:guest-1": [
            {"role": "user", "content": "Tell me about Inception"},
            {"role": "assistant", "content": "It explores dreams."},
        ],
    }

    async def cache_get(key, _ttl):
        return cache.get(key)

    async def cache_set(key, value, *_args, **_kwargs):
        cache[key] = value

    monkeypatch.setattr(shared_state, "cache_get", cache_get)
    monkeypatch.setattr(shared_state, "cache_set", cache_set)

    request_scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/me/adopt-guest",
        "headers": [(b"cookie", f"guest_session_id={guest_id}".encode())],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
    }

    first = await auth_router._adopt_guest_state(Request(request_scope), Response(), user_id)

    assert first["status"] == "success"
    assert first["watchlist_items"] == 1
    assert first["conversations"] == 1
    assert cache[f"guest-watchlist:{guest_id}"] == []
    assert cache[f"guest-conversation:{guest_id}:guest-1"] == []

    second = await auth_router._adopt_guest_state(Request(request_scope), Response(), user_id)

    assert second["status"] == "already_adopted"
    assert second["watchlist_items"] == 1
    assert second["conversations"] == 1

    with factory() as db:
        assert db.query(GuestAdoption).filter_by(user_id=user_id, guest_session_id=guest_id).count() == 1
        assert db.query(WatchlistItem).filter_by(user_id=user_id).count() == 1
        assert db.query(Conversation).filter_by(user_id=user_id).count() == 1
        assert db.query(Movie).filter_by(tmdb_id=27205).count() == 1

    engine.dispose()
