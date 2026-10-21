"""Register -> background delivery job -> verify, on the real database path.

Regression for the 2026-10-12 report "entering the sign-up code always fails".
Exercises the same code the owner runs: the OTP is enqueued as an encrypted
delivery job, the background worker decrypts it and "sends" it, and the user
submits exactly that code.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.jobs import process_background
from app.routers import auth as auth_router


@pytest.mark.asyncio
async def test_emailed_code_verifies_in_production(monkeypatch):
    emailed = []

    async def record_email(to_email, otp, purpose="verification"):
        emailed.append((to_email, otp, purpose))

    async def allow(*_args, **_kwargs):
        return True

    async def no_breach(_password):
        return False

    monkeypatch.setattr(type(main.settings), "is_production", property(lambda _self: True))
    monkeypatch.setattr(main, "validate_csrf", lambda _request: True)
    monkeypatch.setattr(auth_router.shared_state, "rate_limit", allow)
    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    monkeypatch.setattr(process_background, "send_otp_email", record_email)

    email = f"otp-{uuid.uuid4().hex[:10]}@example.com"
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="https://testserver") as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": email.upper(), "password": "a-long-passphrase-for-otp"},
        )
        assert registered.status_code == 200, registered.text
        assert await asyncio.to_thread(process_background.process_otp_jobs_once, "test-worker") >= 1
        sent = [item for item in emailed if item[0] == email]
        assert len(sent) == 1
        verified = await client.post("/api/v1/auth/verify", json={"email": email, "code": sent[0][1]})
        assert verified.status_code == 200, verified.text


@pytest.mark.asyncio
async def test_verification_succeeds_for_a_visitor_who_chatted_as_a_guest_first(monkeypatch):
    """The production guest cookie is signed; adoption must accept it, and an
    adoption problem must never fail an otherwise successful verification."""
    from app.guest_session import guest_cookie_value

    emailed = []

    async def record_email(to_email, otp, purpose="verification"):
        emailed.append((to_email, otp))

    async def allow(*_args, **_kwargs):
        return True

    async def no_breach(_password):
        return False

    monkeypatch.setattr(type(main.settings), "is_production", property(lambda _self: True))
    monkeypatch.setattr(main, "validate_csrf", lambda _request: True)
    monkeypatch.setattr(auth_router.shared_state, "rate_limit", allow)
    monkeypatch.setattr(auth_router, "is_password_breached", no_breach)
    monkeypatch.setattr(process_background, "send_otp_email", record_email)

    guest_id = str(uuid.uuid4())
    await auth_router.shared_state.cache_set(
        f"guest-watchlist:{guest_id}", [{"tmdb_id": 27205, "movie_title": "Inception"}], 1000, ttl=3600
    )
    email = f"guest-{uuid.uuid4().hex[:10]}@example.com"
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="https://testserver") as client:
        client.cookies.set("guest_session_id", guest_cookie_value(guest_id))
        assert (
            await client.post(
                "/api/v1/auth/register", json={"email": email, "password": "a-long-passphrase-for-otp"}
            )
        ).status_code == 200
        await asyncio.to_thread(process_background.process_otp_jobs_once, "test-worker")
        code = next(otp for to, otp in emailed if to == email)
        verified = await client.post("/api/v1/auth/verify", json={"email": email, "code": code})
        assert verified.status_code == 200, verified.text
        state = await client.get("/api/v1/state")
        assert state.status_code == 200
        assert [item["tmdb_id"] for item in state.json()["watchlist"]] == [27205]


def test_guest_cookie_round_trip_and_forgery():
    from app.guest_session import guest_cookie_value, parse_guest_cookie

    session_id = str(uuid.uuid4())
    assert parse_guest_cookie(session_id) == session_id  # development: bare id
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(main.settings), "is_production", property(lambda _self: True))
        signed = guest_cookie_value(session_id)
        assert parse_guest_cookie(signed) == session_id
        assert parse_guest_cookie(session_id) is None
        assert parse_guest_cookie(f"{uuid.uuid4()}.{signed.split('.', 1)[1]}") is None
        assert parse_guest_cookie("not-a-uuid.abc") is None
