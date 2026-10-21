import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import Response
from starlette.requests import Request

if "passlib" not in sys.modules:
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")

    class _CryptContext:
        def __init__(self, *args, **kwargs):
            pass

        def hash(self, value):
            return value

        def verify(self, value, encoded):
            return value == encoded

    context.CryptContext = _CryptContext
    passlib.context = context
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context

from app.database import User
from app.routers import auth as auth_router


def _request(path):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_change_password_hashing_and_verification_run_outside_db_transaction(
    monkeypatch,
):
    transaction_active = False
    verify_calls = 0
    hash_calls = 0

    current = SimpleNamespace(id=7, password_hash="old-hash")

    class DB:
        def get(self, model, user_id):
            assert model is User
            return current if user_id == 7 else None

        def execute(self, _statement):
            return None

        def commit(self):
            return None

    async def fake_run_db(callback):
        nonlocal transaction_active
        transaction_active = True
        try:
            return callback(DB())
        finally:
            transaction_active = False

    async def fake_verify(_password, _encoded):
        nonlocal verify_calls
        assert not transaction_active
        verify_calls += 1
        return True

    async def fake_hash(_password):
        nonlocal hash_calls
        assert not transaction_active
        hash_calls += 1
        return "new-hash"

    async def noop(*_args):
        return None

    monkeypatch.setattr(auth_router, "run_db", fake_run_db)
    monkeypatch.setattr(auth_router, "async_verify_password", fake_verify)
    monkeypatch.setattr(auth_router, "async_hash_password_argon2id", fake_hash)
    monkeypatch.setattr(auth_router, "_assert_password_not_breached", noop)

    payload = auth_router.PasswordChangeRequest(
        current_password="old-password",
        new_password="new-password-123",
    )

    result = await auth_router.change_password(
        _request("/api/v1/auth/me/password"),
        payload,
        current,
    )

    assert result["status"] == "success"
    assert verify_calls == 1
    assert hash_calls == 1
    assert current.password_hash == "new-hash"


@pytest.mark.asyncio
async def test_register_hashing_runs_before_transactional_insert(monkeypatch):
    transaction_active = False
    hash_calls = 0

    class DB:
        def __init__(self):
            self._next = 42

        def add(self, account):
            account.id = self._next

        def flush(self):
            return None

        def commit(self):
            return None

    async def fake_run_db(callback):
        nonlocal transaction_active
        transaction_active = True
        try:
            return callback(DB())
        finally:
            transaction_active = False

    async def fake_hash(_password):
        nonlocal hash_calls
        assert not transaction_active
        hash_calls += 1
        return "hashed-password"

    def fake_otp_store(*_args):
        return None

    monkeypatch.setattr(auth_router, "run_db", fake_run_db)
    monkeypatch.setattr(auth_router, "async_hash_password_argon2id", fake_hash)

    async def no_user(_email):
        return None

    async def noop(*_args):
        return None

    monkeypatch.setattr(auth_router, "_get_user_by_email", no_user)
    monkeypatch.setattr(auth_router, "_check_otp_ip_budget", noop)
    monkeypatch.setattr(auth_router, "_check_auth_attempt_budget", noop)
    monkeypatch.setattr(auth_router, "_assert_password_not_breached", noop)
    monkeypatch.setattr(auth_router, "is_password_allowed", lambda _password: (True, None))
    monkeypatch.setattr(auth_router, "hash_otp", lambda _code: "otp-hash")
    monkeypatch.setattr(auth_router, "_store_otp", fake_otp_store)
    monkeypatch.setattr(auth_router, "_deliver_otp_email", noop)

    payload = auth_router.RegisterRequest(email="transaction@example.test", password="valid-password-123")
    result = await auth_router.register_phase2(payload, _request("/api/v1/auth/register"), Response())

    assert result["status"] == "accepted"
    assert hash_calls == 1
