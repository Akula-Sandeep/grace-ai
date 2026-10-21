import asyncio
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from starlette.requests import Request

from app.config import settings
from app.main import app
from app.security import set_csrf_cookie, validate_csrf


@pytest.fixture
def isolated_database(tmp_path):
    from app.database import Base, SessionLocal

    engine = create_engine(f"sqlite:///{tmp_path / 'auth_sessions.db'}")
    original_bind = SessionLocal.kw["bind"]
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        SessionLocal.configure(bind=original_bind)
        engine.dispose()


@pytest.mark.asyncio
async def test_session_management_requires_authentication():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        listed = await client.get("/api/v1/auth/me/sessions")
        revoked = await client.delete("/api/v1/auth/me/sessions/1")

    assert listed.status_code == 401
    assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_expired_access_token_has_distinct_problem_code():
    import jwt

    from app.config import settings

    token = jwt.encode(
        {
            "sub": "1",
            "iat": datetime.now(UTC) - timedelta(minutes=20),
            "exp": datetime.now(UTC) - timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        client.cookies.set("__Host-access_token", token)
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "token_expired"


@pytest.mark.asyncio
async def test_refresh_accepts_sqlite_naive_expiry_values(isolated_database):
    from app.database import RefreshSession, SessionLocal, User
    from app.routers.auth import _hash_refresh_token

    token_value = uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(
            username="sqlite-refresh-user",
            email="sqlite-refresh@example.test",
            email_verified=True,
            status="verified",
            password_hash="unused",
        )
        db.add(user)
        db.flush()
        db.add(
            RefreshSession(
                user_id=user.id,
                refresh_hash=_hash_refresh_token(token_value),
                idle_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                absolute_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
            )
        )
        db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        client.cookies.set("__Host-refresh_token", token_value)
        response = await client.post("/api/v1/auth/refresh")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_optional_auth_treats_expired_cookie_as_guest_and_clears_it(monkeypatch):
    import jwt
    from starlette.requests import Request
    from starlette.responses import Response

    import app.security as security

    token = jwt.encode(
        {
            "sub": "1",
            "iat": datetime.now(UTC) - timedelta(minutes=20),
            "exp": datetime.now(UTC) - timedelta(minutes=5),
        },
        security.settings.jwt_secret,
        algorithm=security.settings.jwt_algorithm,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/chat",
            "headers": [(b"cookie", f"__Host-access_token={token}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )
    response = Response()

    assert await security.get_current_user_optional(request, response) is None
    assert "__Host-access_token=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_authenticated_profile_exposes_access_token_expiry_hint(monkeypatch):
    from app.database import User
    from app.security import get_current_user

    async def authenticated_user():
        return User(id=1, username="expiry-user", email="expiry@example.test")

    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/api/v1/auth/me")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert int(response.headers["X-Access-Token-Expires-At"]) > int(datetime.now(UTC).timestamp())


@pytest.mark.asyncio
async def test_profile_update_rejects_email_and_validates_username(isolated_database):
    from app.database import SessionLocal, User
    from app.security import get_current_user

    with SessionLocal() as db:
        user = User(
            username="profile-user",
            email="profile@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        other = User(
            username="TakenName",
            email="other@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        db.add_all([user, other])
        db.commit()
        db.refresh(user)
        user_id = user.id

    async def authenticated_user():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            email_change = await client.patch(
                "/api/v1/auth/me",
                json={"email": "new@example.test"},
            )
            invalid_username = await client.patch(
                "/api/v1/auth/me",
                json={"username": "not a valid username"},
            )
            conflicting_username = await client.patch(
                "/api/v1/auth/me",
                json={"username": "takenname"},
            )
            updated = await client.patch(
                "/api/v1/auth/me",
                json={"username": "New_Profile"},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert email_change.status_code == 422
    assert invalid_username.status_code == 422
    assert conflicting_username.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["username"] == "New_Profile"
    with SessionLocal() as db:
        current = db.get(User, user_id)
        assert current.username == "New_Profile"
        assert current.email == "profile@example.test"
        assert current.email_verified is True


@pytest.mark.asyncio
async def test_adopt_guest_migrates_watchlist_and_history_once(isolated_database, monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response

    from app.database import Conversation, SessionLocal, User, WatchlistItem
    from app.routers.auth import adopt_guest_route as adopt_guest
    from app.shared_state import shared_state

    with SessionLocal() as db:
        user = User(
            username="adopter",
            email="adopter@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    guest_id = str(uuid.uuid4())
    cache = {
        f"guest-watchlist:{guest_id}": [{"tmdb_id": 27205, "movie_title": "Inception"}],
        f"guest-interactions:{guest_id}": {"conversation_id": "guest-conversation"},
        f"guest-conversation:{guest_id}:guest-conversation": [
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
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/me/adopt-guest",
            "headers": [(b"cookie", f"guest_session_id={guest_id}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )
    response = Response()
    with SessionLocal() as db:
        current_user = db.get(User, user.id)
        result = await adopt_guest(request, response, current_user)
    assert result["status"] == "success"
    assert result["watchlist_items"] == 1
    assert result["conversations"] == 1

    with SessionLocal() as db:
        assert db.scalar(select(WatchlistItem).where(WatchlistItem.user_id == user.id)) is not None
        assert db.scalar(select(Conversation).where(Conversation.user_id == user.id)) is not None

    second_response = Response()
    with SessionLocal() as db:
        current_user = db.get(User, user.id)
        second = await adopt_guest(request, second_response, current_user)
    assert second["status"] == "already_adopted"


@pytest.mark.asyncio
async def test_csrf_bootstrap_returns_a_double_submit_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/api/v1/auth/csrf")

    token = response.headers.get("x-csrf-token")
    assert response.status_code == 200
    assert token
    assert "csrf_token=" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "source_header"),
    [
        ("http://localhost:5173", "Origin"),
        ("http://127.0.0.1:5173", "Origin"),
        ("http://localhost:5173", "Referer"),
        ("http://127.0.0.1:5173", "Referer"),
    ],
)
async def test_csrf_bootstrap_token_allows_guest_mutations_from_local_origins(
    monkeypatch, origin, source_header
):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "csrf_key", "test-csrf-signing-key-at-least-32")
    monkeypatch.setattr(
        settings,
        "cors_origins",
        "http://localhost:5173, http://127.0.0.1:5173",
    )
    headers = {source_header: f"{origin}/" if source_header == "Referer" else origin}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        csrf_response = await client.get("/api/v1/auth/csrf")
        headers["X-CSRF-Token"] = csrf_response.headers["x-csrf-token"]
        response = await client.post("/api/v1/guest/session", headers=headers)

    assert csrf_response.status_code == 200
    assert response.status_code == 200
    assert response.json()["guest_session_id"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "http://localhost:80",
        "http://127.0.0.1",
        "http://127.0.0.1:80",
    ],
)
def test_production_csrf_allows_docker_frontend_origin_aliases(monkeypatch, origin):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "csrf_key", "test-csrf-signing-key-at-least-32")
    monkeypatch.setattr(
        settings,
        "cors_origins",
        "http://localhost:5173, http://127.0.0.1:5173",
    )
    response = Response()
    csrf_token = set_csrf_cookie(response, "test-nonce")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [
                (b"origin", origin.encode()),
                (b"cookie", f"__Host-csrf_token={csrf_token}".encode()),
                (b"x-csrf-token", csrf_token.encode()),
            ],
        }
    )

    assert validate_csrf(request)
    assert f"__Host-csrf_token={csrf_token}" in response.headers["set-cookie"]
    assert "secure" in response.headers["set-cookie"].lower()


def test_development_csrf_cookie_uses_nonsecure_compatible_name(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    response = Response()

    set_csrf_cookie(response, "test-nonce")

    set_cookie = response.headers["set-cookie"]
    assert "csrf_token=" in set_cookie
    assert "__Host-csrf_token=" not in set_cookie
    assert "secure" not in set_cookie.lower()


@pytest.mark.parametrize(
    ("origin", "source_header"),
    [
        ("http://localhost:4173", "origin"),
        ("http://127.0.0.1:8080", "origin"),
        ("http://localhost:4173", "referer"),
        ("http://127.0.0.1:8080", "referer"),
    ],
)
def test_csrf_validation_accepts_local_development_ports(monkeypatch, origin, source_header):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "csrf_key", "test-csrf-signing-key-at-least-32")
    monkeypatch.setattr(settings, "cors_origins", "http://localhost:5173")
    monkeypatch.setattr(
        settings,
        "cors_origin_regex",
        r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    )
    response = Response()
    csrf_token = set_csrf_cookie(response, "test-nonce")
    source = f"{origin}/search" if source_header == "referer" else origin
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [
                (source_header.encode(), source.encode()),
                (b"cookie", f"__Host-csrf_token={csrf_token}".encode()),
                (b"x-csrf-token", csrf_token.encode()),
            ],
        }
    )

    assert validate_csrf(request)


@pytest.mark.asyncio
async def test_personalization_toggle_and_export_archive(isolated_database):
    from app.database import Memory, Preference, SessionLocal, User
    from app.memory import MemoryService
    from app.security import get_current_user, hash_password

    username = f"phase5_{uuid.uuid4().hex[:12]}"
    email = f"{username}@example.test"
    with SessionLocal() as db:
        user = User(
            username=username,
            email=email,
            email_verified=True,
            status="verified",
            password_hash=hash_password("phase-five-test-password"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
        memory = Memory(
            user_id=user_id,
            content="Avoids graphic violence.",
            memory_type="preference",
        )
        preference = Preference(
            user_id=user_id,
            dimension="genre",
            value="slow-burn mysteries",
            polarity="prefer",
        )
        db.add_all([memory, preference])
        db.commit()
        memory_id = memory.id
        preference_id = preference.id

    async def authenticated_user():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            default_setting = await client.get("/api/v1/auth/me/personalization")
            disabled = await client.patch(
                "/api/v1/auth/me/personalization",
                json={"memory_enabled": False},
            )
            export_request = await client.post("/api/v1/auth/me/export")
            from app.services.export_service import generate_export_job

            await asyncio.to_thread(
                generate_export_job,
                export_request.json()["job_id"],
                worker_id="test-worker",
            )
            export_status = await client.get(f"/api/v1/auth/me/export/{export_request.json()['job_id']}")
            exported = await client.get(export_status.json()["download_url"])
            default_ui_settings = await client.get(
                "/api/v1/auth/me/ui-settings",
                headers={"Accept-Language": "nl-NL,nl;q=0.9"},
            )
            updated_ui_settings = await client.patch(
                "/api/v1/auth/me/ui-settings",
                json={"theme": "dark", "density": "compact", "region": "gb"},
            )
            invalid_ui_settings = await client.patch(
                "/api/v1/auth/me/ui-settings",
                json={"theme": "neon"},
            )
            updated_preference = await client.patch(
                f"/api/v1/auth/me/preferences/{preference_id}",
                json={"value": "thoughtful mysteries"},
            )
            deleted_memory = await client.delete(f"/api/v1/auth/me/memories/{memory_id}")
            cleared = await client.post("/api/v1/auth/me/personalization/clear")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    defaults = {"answer_style": "balanced", "daily_brief_enabled": False}
    assert default_setting.json() == {
        "memory_enabled": True,
        "reminders_enabled": True,
        **defaults,
    }
    assert disabled.json() == {
        "memory_enabled": False,
        "reminders_enabled": True,
        **defaults,
    }
    with SessionLocal() as db:
        assert MemoryService(db).memories(user_id) == []
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert export_request.status_code == 200
    assert export_status.json()["job_status"] == "ready"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        account = json.loads(archive.read("account.json"))
        memories = [
            json.loads(line) for line in archive.read("memories.ndjson").decode().splitlines() if line
        ]
    assert account["account"]["email"] == email
    assert {
        "conversations.ndjson",
        "watchlist.ndjson",
        "watched.ndjson",
        "ratings.ndjson",
        "preferences.ndjson",
        "memories.ndjson",
    } <= names
    assert memories[0]["content"] == "Avoids graphic violence."
    assert default_ui_settings.json() == {
        "theme": "system",
        "density": "comfortable",
        "region": "NL",
        "motion": "system",
    }
    assert updated_ui_settings.json() == {
        "theme": "dark",
        "density": "compact",
        "region": "GB",
        "motion": "system",
    }
    assert invalid_ui_settings.status_code == 422
    assert updated_preference.json()["value"] == "thoughtful mysteries"
    assert deleted_memory.status_code == 200
    assert cleared.status_code == 200


@pytest.mark.asyncio
async def test_profile_display_name_is_free_form_and_normalised(isolated_database):
    """The greeting uses display_name, not the restricted unique username handle."""
    from app.database import SessionLocal, User
    from app.security import get_current_user

    with SessionLocal() as db:
        user = User(
            username="user_abc123",
            email="name@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id

    async def authenticated_user():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            before = await client.get("/api/v1/auth/me")
            updated = await client.patch("/api/v1/auth/me", json={"display_name": "  Anpreet   Kaur "})
            too_long = await client.patch("/api/v1/auth/me", json={"display_name": "x" * 61})
            control = await client.patch("/api/v1/auth/me", json={"display_name": "bad\u0007name"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert before.json()["display_name"] is None
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Anpreet Kaur"
    assert updated.json()["username"] == "user_abc123"
    assert too_long.status_code == 422
    assert control.status_code == 422
