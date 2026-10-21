from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, OTPVerification
from app.routers import auth as auth_router


@pytest.fixture
def auth_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(auth_router, "SessionLocal", test_sessions)
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-long-enough-for-otp-tests")

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    monkeypatch.setattr(auth_router.shared_state, "rate_limit", allow_rate_limit)
    yield test_sessions
    engine.dispose()


def test_otp_attempts_lock_code_and_expired_codes_are_removed(auth_db, monkeypatch):

    monkeypatch.setattr(auth_router, "verify_otp", lambda *_args: False)
    with auth_db() as db:
        db.add(
            OTPVerification(
                email="locked@example.com",
                otp_hash="test-hash",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                attempts=0,
            )
        )
        db.add(
            OTPVerification(
                email="expired@example.com",
                otp_hash="test-hash",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
                attempts=0,
            )
        )
        db.commit()

    for _ in range(auth_router.OTP_ATTEMPT_LIMIT):
        _user_id, result = auth_router._consume_otp("locked@example.com", "000000")
        assert result == "invalid"
    _user_id, result = auth_router._consume_otp("locked@example.com", "000000")
    assert result == "locked"
    _user_id, result = auth_router._consume_otp("expired@example.com", "000000")
    assert result == "expired"

    with auth_db() as db:
        assert (
            db.scalar(select(OTPVerification).where(OTPVerification.email == "expired@example.com")) is None
        )
