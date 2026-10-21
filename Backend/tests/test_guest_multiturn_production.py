"""Regression: guest conversations must survive more than one turn in production.

Root cause found 2026-10-12 from the owner's screenshots (first guest answer
works, every later turn fails with a generic error and Retry never recovers):
in production the guest_session_id cookie is HMAC-signed, but /chat/stream
re-set it on the StreamingResponse with the *bare* session id. The next
request's cookie then failed signature validation, a brand-new guest session
was minted, and the existing conversation lookup returned 404.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import main


@pytest.mark.asyncio
async def test_guest_conversation_continues_across_turns_in_production(monkeypatch):
    cached = {}

    async def allow(*_args, **_kwargs):
        return True

    async def cache_set(key, value, _max_entries, ttl=None):
        cached[key] = value

    async def cache_get(key, _ttl):
        return cached.get(key)

    seen_history_lengths = []

    async def guest_stream(
        message, user_id, conversation_id, guest_history, guest_session_id=None, **_kwargs
    ):
        seen_history_lengths.append(len(guest_history))
        yield f"answer to {message}"

    monkeypatch.setattr(type(main.settings), "is_production", property(lambda _self: True))
    monkeypatch.setattr(main, "validate_csrf", lambda _request: True)
    monkeypatch.setattr(main.shared_state, "rate_limit", allow)
    monkeypatch.setattr(main.shared_state, "cache_set", cache_set)
    monkeypatch.setattr(main.shared_state, "cache_get", cache_get)
    monkeypatch.setattr(main.agent, "stream", guest_stream)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="https://testserver") as client:
        await client.post("/api/v1/guest/session")
        conversation_id = None
        for turn in range(6):
            response = await client.post(
                "/api/v1/chat/stream",
                json={"message": f"turn {turn}", "conversation_id": conversation_id},
            )
            assert response.status_code == 200, (turn, response.status_code, response.text[:200])
            assert "event: done" in response.text, (turn, response.text[:300])
            started = json.loads(response.text.splitlines()[1][6:])
            conversation_id = conversation_id or started["conversation_id"]
            assert started["conversation_id"] == conversation_id

    assert seen_history_lengths == [0, 2, 4, 6, 8, 10]


@pytest.mark.asyncio
async def test_empty_answer_stream_is_a_retryable_failure_not_no_evidence(monkeypatch):
    """A stream that yields no text (e.g. provider returned nothing for "hi")
    used to be reported as "I couldn't find reliable evidence for that specific
    claim" with retryable=False -- wrong copy, and it hid the Retry path."""
    cached = {}

    async def allow(*_args, **_kwargs):
        return True

    async def cache_set(key, value, _max_entries, ttl=None):
        cached[key] = value

    async def cache_get(key, _ttl):
        return cached.get(key)

    async def silent_stream(*_args, **_kwargs):
        if False:
            yield ""

    monkeypatch.setattr(main.shared_state, "rate_limit", allow)
    monkeypatch.setattr(main.shared_state, "cache_set", cache_set)
    monkeypatch.setattr(main.shared_state, "cache_get", cache_get)
    monkeypatch.setattr(main.agent, "stream", silent_stream)
    monkeypatch.setattr(main, "validate_csrf", lambda _request: True)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://testserver") as client:
        response = await client.post("/api/v1/chat/stream", json={"message": "hi"})
    frames = [block for block in response.text.split("\n\n") if block.startswith("event: error")]
    assert len(frames) == 1
    payload = json.loads(frames[0].split("data: ", 1)[1])
    assert payload["code"] == "unknown"
    assert payload["retryable"] is True
    assert "evidence" not in payload["message"]
