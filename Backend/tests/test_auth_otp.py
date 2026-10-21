from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.auth import jwt, verify_otp
from app.config import settings
from app.database import Base, Movie, OTPVerification, User, WatchedItem, WatchlistItem
from app.main import app
from app.movie_state import MovieStateService
from app.routers import auth as auth_router
from app.services.email_service import send_otp_email


def _recording_delivery(sent):
    async def deliver(email, code, purpose="verification"):
        sent.append((email, code))

    return deliver


def _access_cookie(client):
    return client.cookies.get("__Host-access_token") or client.cookies.get("access_token")


@pytest.fixture
def auth_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(auth_router, "SessionLocal", sessions)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-long-enough-for-otp-tests")

    async def allow_rate_limit(*_args, **_kwargs):
        return True

    monkeypatch.setattr(auth_router.shared_state, "rate_limit", allow_rate_limit)
    yield sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_breach_check_network_failure_is_reported(monkeypatch):
    import httpx

    from app import security

    events = []

    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError(
            "offline",
            request=httpx.Request("GET", "https://api.pwnedpasswords.com/range/00000"),
        )

    class UnavailableClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return unavailable()

    monkeypatch.setattr(security.httpx, "AsyncClient", UnavailableClient)
    monkeypatch.setattr(
        security,
        "log_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert not await security.is_password_breached("a-long-test-password")
    assert events[0][0][0] == "password_breach_check_unavailable"
    assert events[0][1]["details"]["reason"] == "provider_unavailable"


@pytest.mark.asyncio
async def test_registration_verification_cooldown_and_session(auth_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        auth_router,
        "_deliver_otp_email",
        _recording_delivery(sent),
    )

    async def no_breach(_password):
        return False

    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    registration = {
        "email": "MovieFan@example.com",
        "password": "a-long-passphrase-for-phase-two",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.9", 8765)),
        base_url="http://test",
    ) as client:
        requested = await client.post("/api/v1/auth/register", json=registration)
        assert requested.status_code == 200
        assert "message" in requested.json()
        assert sent[0][0] == "moviefan@example.com"
        assert len(sent[0][1]) == 6

        recent = await client.post("/api/v1/auth/resend", json={"email": "moviefan@example.com"})
        assert recent.status_code == 200

        verified = await client.post(
            "/api/v1/auth/verify",
            json={"email": "MOVIEFAN@example.com", "code": sent[0][1]},
        )
        cookie = _access_cookie(client)
        profile_response = await client.get("/api/v1/auth/me")
        replay = await client.post(
            "/api/v1/auth/verify",
            json={"email": "moviefan@example.com", "code": sent[0][1]},
        )
        logout_response = await client.post("/api/v1/auth/logout")

    assert verified.status_code == 200
    assert replay.status_code == 400
    result = verified.json()
    assert "access_token" not in result and "token" not in result
    assert cookie
    assert profile_response.status_code == 200
    assert profile_response.json()["email"] == "moviefan@example.com"
    assert logout_response.status_code == 200
    assert "max-age=0" in logout_response.headers["set-cookie"].lower()
    assert _access_cookie(client) is None
    claims = jwt.decode(cookie, settings.jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == str(profile_response.json()["id"])
    with auth_db() as db:
        record = db.scalar(select(OTPVerification))
        assert record is not None and record.is_used
        user = db.scalar(select(User).where(User.email == "moviefan@example.com"))
        assert user is not None


@pytest.mark.asyncio
async def test_registered_password_logs_in_after_verification_and_wrong_password_is_rejected(
    auth_db,
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(
        auth_router,
        "_deliver_otp_email",
        _recording_delivery(sent),
    )

    async def no_breach(_password):
        return False

    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    password = "".join(("a-long-passphrase", "-for-login"))
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.8", 8765)),
        base_url="http://test",
    ) as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": "login@example.com", "password": password},
        )
        verified = await client.post(
            "/api/v1/auth/verify",
            json={"email": "login@example.com", "code": sent[0][1]},
        )
        await client.post("/api/v1/auth/logout")
        logged_in = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@example.com", "password": password},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@example.com", "password": "wrong-password-long"},
        )

    assert registered.status_code == 200
    assert verified.status_code == 200
    assert logged_in.status_code == 200
    assert "access_token=" in logged_in.headers.get("set-cookie", "")
    assert "refresh_token=" in logged_in.headers.get("set-cookie", "")
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_budget_is_scoped_to_ip_and_account(auth_db, monkeypatch):
    from app.security import hash_password_argon2id

    with auth_db() as db:
        db.add(
            User(
                username="throttle-user",
                email="throttle@example.com",
                password_hash=hash_password_argon2id("correct-password-long"),
                email_verified=True,
                status="verified",
            )
        )
        db.commit()

    counts = {}

    async def rate_limit(key, _limit, _window):
        counts[key] = counts.get(key, 0) + 1
        return counts[key] <= 25 if "auth-ip-account:" in key else True

    expected_password = "".join(("correct-password", "-long"))

    async def verify_for_test(password, _user):
        return password == expected_password

    monkeypatch.setattr(auth_router.shared_state, "rate_limit", rate_limit)
    monkeypatch.setattr(auth_router, "_equalize_auth_timing", verify_for_test)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.10", 8765)),
        base_url="http://test",
    ) as attacker:
        for _ in range(25):
            response = await attacker.post(
                "/api/v1/auth/login",
                json={
                    "email": "throttle@example.com",
                    "password": "wrong-password-long",
                },
            )
            assert response.status_code == 401

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.11", 8765)),
        base_url="http://test",
    ) as victim:
        response = await victim.post(
            "/api/v1/auth/login",
            json={"email": "throttle@example.com", "password": expected_password},
        )

    assert response.status_code == 200


def test_otp_hash_is_not_plaintext_and_attempt_limit_invalidates(auth_db, monkeypatch):
    code = "042691"
    hashed = auth_router.hash_otp(code)
    assert hashed != code
    assert verify_otp(code, hashed)

    with auth_db() as db:
        record = OTPVerification(
            email="locked@example.com",
            otp_hash=hashed,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            created_at=datetime.now(UTC),
            attempts=0,
            is_used=False,
        )
        db.add(record)
        db.commit()

    monkeypatch.setattr(auth_router, "verify_otp", lambda *_args: False)
    for _ in range(auth_router.OTP_ATTEMPT_LIMIT):
        _uid, result = auth_router._consume_otp("locked@example.com", "000000")
        assert result == "invalid"

    _uid, result = auth_router._consume_otp("locked@example.com", "000000")
    assert result == "locked"
    with auth_db() as db:
        record = db.scalar(select(OTPVerification).where(OTPVerification.email == "locked@example.com"))
        assert record is not None and record.is_used
        assert record.attempts == auth_router.OTP_ATTEMPT_LIMIT + 1


def test_access_token_cookie_is_secure_only_in_production(monkeypatch):
    from fastapi import Response

    from app.security import set_access_token_cookie

    monkeypatch.setattr(settings, "environment", "production")
    response = Response()
    set_access_token_cookie(response, "signed-token")
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie


@pytest.mark.asyncio
async def test_email_service_uses_development_fallback_when_smtp_missing(monkeypatch):
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_email", None)
    monkeypatch.setattr(settings, "smtp_password", None)
    await send_otp_email("offline@example.com", "123456")


def test_watchlist_and_watched_fetches_are_user_scoped(auth_db, monkeypatch):
    monkeypatch.setattr(main, "SessionLocal", auth_db)
    with auth_db() as db:
        user_one = User(username="first", password_hash="hash")
        user_two = User(username="second", password_hash="hash")
        movie_one = Movie(tmdb_id=1001, title="First film")
        movie_two = Movie(tmdb_id=1002, title="Second film")
        db.add_all([user_one, user_two, movie_one, movie_two])
        db.commit()
        db.add_all(
            [
                WatchlistItem(user_id=user_one.id, movie_id=movie_one.id),
                WatchlistItem(user_id=user_two.id, movie_id=movie_two.id),
                WatchedItem(user_id=user_one.id, movie_id=movie_one.id),
                WatchedItem(user_id=user_two.id, movie_id=movie_two.id),
            ]
        )
        db.commit()

        service = MovieStateService(db, tmdb=None)
        assert [item["title"] for item in service.list_watchlist(user_one.id)] == ["First film"]
        assert [item["title"] for item in service.list_watched(user_two.id)] == ["Second film"]
        assert [item["title"] for item in service.list_watched(user_one.id)] == ["First film"]

        profile = main.state(user_one)
        assert len(profile["watchlist"]) == 1
        assert len(profile["watched"]) == 1


def _counting_rate_limiter():
    """A real fixed-window counter, so budget behaviour is exercised for real."""
    counts = {}

    async def rate_limit(key, limit, _window=60, *_args, **_kwargs):
        counts[key] = counts.get(key, 0) + 1
        return counts[key] <= limit

    return rate_limit, counts


@pytest.mark.asyncio
async def test_a_normal_signup_with_typos_and_a_resend_is_never_locked_out(auth_db, monkeypatch):
    """Regression (2026-10-12): sending and checking codes shared one budget of
    5 requests per IP per hour, so register + resend + a couple of mistyped
    codes locked verification for an hour ("the code always fails")."""
    sent = []
    monkeypatch.setattr(auth_router, "_deliver_otp_email", _recording_delivery(sent))

    async def no_breach(_password):
        return False

    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    limiter, counts = _counting_rate_limiter()
    monkeypatch.setattr(auth_router.shared_state, "rate_limit", limiter)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("172.18.0.1", 1)), base_url="http://test"
    ) as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": "typo@example.com", "password": "a-long-passphrase-for-typos"},
        )
        resent = await client.post("/api/v1/auth/resend", json={"email": "typo@example.com"})
        wrong = [
            await client.post("/api/v1/auth/verify", json={"email": "typo@example.com", "code": guess})
            for guess in ("000000", "111111", "222222")
        ]
        verified = await client.post(
            "/api/v1/auth/verify", json={"email": "typo@example.com", "code": sent[-1][1]}
        )

    assert registered.status_code == 200
    assert registered.json()["expires_in_minutes"] == settings.otp_expire_minutes
    assert resent.status_code == 200
    assert [response.status_code for response in wrong] == [400, 400, 400]
    assert all(response.json()["code"] == "otp_invalid" for response in wrong)
    assert "isn’t right" in wrong[0].json()["detail"]
    assert verified.status_code == 200, verified.text
    assert counts.get("otp-send-ip:172.18.0.1") == 2
    assert counts.get("otp-verify-ip:172.18.0.1") == 4


@pytest.mark.asyncio
async def test_verify_explains_expired_and_burned_codes(auth_db, monkeypatch):
    sent = []
    monkeypatch.setattr(auth_router, "_deliver_otp_email", _recording_delivery(sent))

    async def no_breach(_password):
        return False

    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/api/v1/auth/register",
            json={"email": "late@example.com", "password": "a-long-passphrase-for-late"},
        )
        with auth_db() as db:
            record = db.scalar(select(OTPVerification).where(OTPVerification.email == "late@example.com"))
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
        expired = await client.post(
            "/api/v1/auth/verify", json={"email": "late@example.com", "code": sent[-1][1]}
        )

        await client.post(
            "/api/v1/auth/register",
            json={"email": "burn@example.com", "password": "a-long-passphrase-for-burn"},
        )
        attempts = [
            await client.post("/api/v1/auth/verify", json={"email": "burn@example.com", "code": "999999"})
            for _ in range(auth_router.OTP_ATTEMPT_LIMIT + 1)
        ]

    assert expired.status_code == 400
    assert expired.json()["code"] == "otp_expired"
    assert attempts[-1].json()["code"] == "otp_locked"


def test_schema_status_reports_a_database_behind_the_code(monkeypatch):
    from app import main as app_main

    class FakeResult:
        def scalar(self):
            return "0026_notification_links"

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return FakeResult()

    monkeypatch.setattr(app_main, "SessionLocal", FakeSession)
    current, head = app_main._schema_status()
    assert current == "0026_notification_links"
    assert head and head != current  # the code is ahead of this database
