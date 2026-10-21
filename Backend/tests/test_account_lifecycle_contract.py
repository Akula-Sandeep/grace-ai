import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

pytest.importorskip("passlib.context")
pytest.importorskip("aiosmtplib")


from app.database import Base, SessionLocal, User
from app.main import app
from app.routers import auth as auth_router
from app.security import get_current_user


@pytest.fixture
def isolated_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'account_lifecycle.db'}")
    original_bind = SessionLocal.kw["bind"]
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        SessionLocal.configure(bind=original_bind)
        engine.dispose()


@pytest.mark.asyncio
async def test_delete_restore_uses_signed_restore_cookie(isolated_database, monkeypatch):
    with SessionLocal() as db:
        user = User(
            username=f"restore-{uuid.uuid4().hex[:8]}",
            email="restore@example.test",
            email_verified=True,
            status="verified",
            password_hash="stub:correct-password",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    async def current_user():
        with SessionLocal() as db:
            return db.get(User, user_id)

    async def verify(password, _hash):
        return password == "correct-password"

    monkeypatch.setattr(auth_router, "async_verify_password", verify)
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            deleted = await client.request(
                "DELETE",
                "/api/v1/auth/me",
                json={"current_password": "correct-password"},
            )
            assert deleted.status_code == 200
            delete_cookie = deleted.headers.get("set-cookie", "")
            assert "grace_restore_token=" in delete_cookie
            assert "deleted_account_email=" not in delete_cookie
            assert "Path=/api/v1/auth" in delete_cookie

            with SessionLocal() as db:
                assert db.get(User, user_id).status == "deleted"

            restored = await client.post(
                "/api/v1/auth/me/restore",
                json={"current_password": "correct-password"},
            )
            assert restored.status_code == 200
            restore_cookie = restored.headers.get("set-cookie", "")
            assert "grace_restore_token=" in restore_cookie
            assert "Max-Age=0" in restore_cookie

            with SessionLocal() as db:
                account = db.get(User, user_id)
                assert account.status == "verified"
                assert account.is_active is True
                assert account.deleted_at is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_logout_all_never_issues_restore_cookie(isolated_database):
    with SessionLocal() as db:
        user = User(
            username=f"logout-{uuid.uuid4().hex[:8]}",
            email="logout@example.test",
            email_verified=True,
            status="verified",
            password_hash="stub:unused",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    async def current_user():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/v1/auth/logout-all")
        assert response.status_code == 200
        cookie = response.headers.get("set-cookie", "")
        assert "deleted_account_email=" not in cookie
    finally:
        app.dependency_overrides.pop(get_current_user, None)
