import asyncio
import sys
import types

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

from app import database
from app.database import Base, User
from app.routers import auth as auth_router


@pytest.mark.asyncio
async def test_failed_login_counter_is_atomic_under_concurrent_attempts(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth_router, "SessionLocal", factory)

    with factory() as db:
        user = User(username="counter-user", email="counter@example.test", password_hash="hash")
        db.add(user)
        db.commit()

    results = await asyncio.gather(
        *(auth_router._record_failed_login("counter@example.test") for _ in range(20))
    )

    with factory() as db:
        user = db.query(User).filter_by(email="counter@example.test").one()
        assert user.failed_login_attempts == 20
        assert user.locked_until is not None

    assert sum(results) == 11
    engine.dispose()
