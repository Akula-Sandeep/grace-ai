import asyncio
import sys
import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import Response


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


def _request():
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/register",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_concurrent_registration_uses_db_uniqueness_authority(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'registration.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth_router, "SessionLocal", factory)
    monkeypatch.setattr(auth_router, "_check_otp_ip_budget", lambda _request: asyncio.sleep(0))
    monkeypatch.setattr(
        auth_router,
        "_check_auth_attempt_budget",
        lambda _request, _email: asyncio.sleep(0),
    )
    monkeypatch.setattr(auth_router, "_assert_password_not_breached", lambda _password: asyncio.sleep(0))
    monkeypatch.setattr(
        auth_router,
        "async_hash_password_argon2id",
        lambda _password: asyncio.sleep(0, result="hashed"),
    )
    monkeypatch.setattr(auth_router, "hash_otp", lambda _code: "otp-hash")
    monkeypatch.setattr(auth_router, "_store_otp", lambda *_args: None)
    monkeypatch.setattr(auth_router, "_deliver_otp_email", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(auth_router, "is_password_allowed", lambda _password: (True, None))

    checked = 0
    both_checked = asyncio.Event()

    async def synchronized_lookup(_email):
        nonlocal checked
        checked += 1
        if checked == 2:
            both_checked.set()
        await both_checked.wait()
        return None

    monkeypatch.setattr(auth_router, "_get_user_by_email", synchronized_lookup)

    payload = auth_router.RegisterRequest(email="race@example.test", password="valid-password-123")

    async def register_once():
        try:
            return await auth_router.register_phase2(payload, _request(), Response())
        except HTTPException as exc:
            return exc.status_code

    results = await asyncio.gather(register_once(), register_once())

    assert sum(result == 409 for result in results) == 1
    assert sum(isinstance(result, dict) and result.get("status") == "accepted" for result in results) == 1

    with factory() as db:
        assert db.query(User).filter_by(email="race@example.test").count() == 1

    engine.dispose()
