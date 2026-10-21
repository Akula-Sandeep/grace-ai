"""PostgreSQL-only regression coverage for atomic refresh-token rotation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from starlette.requests import Request
from starlette.responses import Response

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_DATABASE_URL is required for the multi-transaction refresh race test",
)


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/refresh",
            "headers": [(b"cookie", f"__Host-refresh_token={token}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "https",
        }
    )


@pytest.mark.asyncio
async def test_concurrent_refresh_token_is_consumed_once(monkeypatch):
    from sqlalchemy import create_engine

    from app.database import Base, RefreshSession, SessionLocal, User
    from app.routers.auth import _hash_refresh_token, refresh_phase2

    engine = create_engine(POSTGRES_TEST_URL, pool_pre_ping=True)
    original_bind = SessionLocal.kw["bind"]
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    try:
        token = uuid.uuid4().hex
        with SessionLocal() as db:
            user = User(
                username=f"refresh-race-{uuid.uuid4().hex[:8]}",
                email=f"refresh-race-{uuid.uuid4().hex[:8]}@example.test",
                email_verified=True,
                status="verified",
                password_hash="unused",
            )
            db.add(user)
            db.flush()
            user_id = user.id
            db.add(
                RefreshSession(
                    user_id=user.id,
                    refresh_hash=_hash_refresh_token(token),
                    idle_expires_at=datetime.now(UTC) + timedelta(hours=1),
                    absolute_expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            db.commit()

        async def call_once():
            return await asyncio.to_thread(refresh_phase2, _request(token), Response())

        results = await asyncio.gather(call_once(), call_once(), return_exceptions=True)
        successes = [x for x in results if not isinstance(x, Exception)]
        failures = [x for x in results if isinstance(x, Exception)]

        assert len(successes) == 1
        assert len(failures) == 1
        assert getattr(failures[0], "status_code", None) == 401

        with SessionLocal() as db:
            # Scoped to this user: other tests share the database and create sessions too.
            session = db.query(RefreshSession).filter(RefreshSession.user_id == user_id).one()
            assert session.revoked_at is not None
            assert session.revoked_reason == "refresh_reuse"
    finally:
        SessionLocal.configure(bind=original_bind)
        engine.dispose()
