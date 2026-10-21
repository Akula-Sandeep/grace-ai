"""First-run onboarding stores explicit preferences; feedback keeps its category."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select

from app.main import app


@pytest.fixture
def user_client(tmp_path):
    from app.database import Base, SessionLocal, User
    from app.security import get_current_user

    engine = create_engine(f"sqlite:///{tmp_path / 'onboarding.db'}")
    original_bind = SessionLocal.kw["bind"]
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    with SessionLocal() as db:
        user = User(username=f"onb_{uuid.uuid4().hex[:10]}", password_hash="x", status="verified")
        db.add(user)
        db.commit()
        user_id = user.id

    async def current():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = current
    try:
        yield user_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_onboarding_saves_explicit_preferences_and_ignores_unknown_values(user_client):
    from app.database import Preference, SessionLocal

    async with _client() as client:
        options = await client.get("/api/v1/auth/me/onboarding")
        assert options.status_code == 200
        assert "Horror" in options.json()["genres"] and "gore" in options.json()["traits"]
        assert options.json()["has_explicit_preferences"] is False
        saved = await client.post(
            "/api/v1/auth/me/onboarding",
            json={
                "favorite_genres": ["science fiction", "Not A Genre"],
                "avoid_genres": ["horror"],
                "avoid_traits": ["jump scares", "made-up trait"],
                "favorite_movies": ["Arrival"],
            },
        )
        again = await client.get("/api/v1/auth/me/onboarding")
    assert saved.status_code == 200
    stored = {
        (row.dimension, row.value, row.polarity, row.source)
        for row in SessionLocal().scalars(select(Preference).where(Preference.user_id == user_client))
    }
    assert stored == {
        ("genre", "Science Fiction", "prefer", "explicit"),
        ("genre", "Horror", "avoid", "explicit"),
        ("trait", "jump scares", "avoid", "explicit"),
        ("movie", "Arrival", "prefer", "explicit"),
    }
    assert again.json()["has_explicit_preferences"] is True


@pytest.mark.asyncio
async def test_onboarding_respects_memory_off(user_client):
    from app.database import Preference, SessionLocal

    async with _client() as client:
        await client.patch("/api/v1/auth/me/personalization", json={"memory_enabled": False})
        saved = await client.post("/api/v1/auth/me/onboarding", json={"favorite_genres": ["drama"]})
    assert saved.json() == {"saved": [], "memory_enabled": False}
    assert SessionLocal().scalars(select(Preference)).first() is None


@pytest.mark.asyncio
async def test_feedback_category_is_stored(user_client):
    from app.database import Feedback, SessionLocal

    async with _client() as client:
        response = await client.post(
            "/api/v1/auth/me/feedback",
            json={"text": "The source for this fact was wrong.", "category": "incorrect_source"},
        )
        bad = await client.post(
            "/api/v1/auth/me/feedback", json={"text": "Something else.", "category": "nonsense"}
        )
    assert response.status_code == 200
    assert bad.status_code == 422
    assert SessionLocal().scalars(select(Feedback)).one().feedback_type == "product:incorrect_source"


@pytest.mark.asyncio
async def test_memory_can_be_corrected_and_shows_provenance(user_client):
    from app.database import Memory, SessionLocal

    with SessionLocal() as db:
        memory = Memory(
            user_id=user_client, content="Often watches late", content_hash="h1", memory_type="episodic"
        )
        db.add(memory)
        db.commit()
        memory_id = memory.id
    async with _client() as client:
        updated = await client.patch(
            f"/api/v1/auth/me/memories/{memory_id}", json={"content": "Usually watches  after 10pm"}
        )
        missing = await client.patch("/api/v1/auth/me/memories/99999", json={"content": "x"})
    assert updated.status_code == 200
    assert updated.json()["content"] == "Usually watches after 10pm"
    assert updated.json()["type"] == "user_stated"
    assert missing.status_code == 404
