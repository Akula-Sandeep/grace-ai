"""Answer style and the daily brief are personal settings the user controls (Settings > Personalisation)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal, User
from app.main import app
from app.security import get_current_user


@pytest.fixture
def signed_in():
    with SessionLocal() as db:
        import uuid

        user = User(username=f"style-{uuid.uuid4().hex[:8]}", password_hash="x", status="active")
        db.add(user)
        db.commit()
        user_id = user.id

    async def current():
        with SessionLocal() as db:
            return db.get(User, user_id)

    app.dependency_overrides[get_current_user] = current
    yield user_id
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_answer_style_and_daily_brief_are_saved_and_validated(signed_in):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        saved = await client.patch(
            "/api/v1/auth/me/personalization", json={"answer_style": "brief", "daily_brief_enabled": True}
        )
        assert saved.status_code == 200
        assert saved.json()["answer_style"] == "brief" and saved.json()["daily_brief_enabled"] is True
        assert (await client.get("/api/v1/auth/me/personalization")).json()["answer_style"] == "brief"
        bad = await client.patch("/api/v1/auth/me/personalization", json={"answer_style": "shouty"})
        assert bad.status_code == 422
        empty = await client.patch("/api/v1/auth/me/personalization", json={})
        assert empty.status_code == 400
