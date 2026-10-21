"""Reliability of model-led turns: stop, duplicate sends, rate limits, memory edits (round 7d).

The model is scripted; what is checked is the application's behaviour around
it: what is saved, what is done, and what the model is told next time.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.inference.models import StreamEvent, ToolCall
from tests.jarvis_harness import Conversation, _events, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, say, system_text


@pytest.fixture
def world(monkeypatch):
    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    convo = Conversation(factory, new_user(factory))
    return convo, model, factory, monkeypatch


def _request(disconnect_after=None):
    from tests.test_chat_runtime import make_request

    return make_request(disconnect_after)


class SlowThenActs(ScriptedModel):
    """Says something, pauses, then asks to change the watchlist."""

    spoke = False

    async def stream_events(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield StreamEvent("text", text="Let me add that for you. ")
            self.spoke = asyncio.get_running_loop().time()
            await asyncio.sleep(0.8)
            yield StreamEvent(
                "tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="update_my_list",
                        arguments={"action": "add_to_watchlist", "movies": [{"title": "Dune"}]},
                    )
                ],
            )
            yield StreamEvent("finish", finish_reason="tool_calls")
        else:
            yield StreamEvent("text", text="Done.")
            yield StreamEvent("finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_stopping_mid_answer_saves_the_partial_reply_and_does_nothing_afterwards(world):
    from app import main
    from app.database import Message

    convo, _, factory, monkeypatch = world
    model = SlowThenActs()
    install_model(monkeypatch, model)
    request = _request()

    async def gone_after_first_words():
        # The first words reach the browser, then the user presses stop while the model pauses.
        await asyncio.sleep(0.05)
        return bool(model.spoke) and asyncio.get_running_loop().time() - model.spoke > 0.3

    request.is_disconnected = gone_after_first_words
    response = await main.chat_stream(
        main.ChatIn(message="could you keep Dune for me for later?", client_message_id=uuid.uuid4()),
        SimpleNamespace(id=convo.user_id),
        request,
    )
    events = await _events(response)
    await asyncio.sleep(1.0)  # anything still running would act now
    assert convo.watchlist() == []
    assert len(model.requests) == 1  # no further model round after the stop
    with factory() as db:
        reply = db.scalar(select(Message).where(Message.uuid_id == events[0]["message_id"]))
        assert reply.status == "stopped"
        assert reply.content.startswith("Let me add that")


@pytest.mark.asyncio
async def test_a_duplicate_send_of_the_same_message_acts_once(world):
    """The same client_message_id twice at once (double click, network retry)."""
    from app import main

    convo, model, _, _ = world
    model.script(
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Dune"}]),
        say("Anything else?"),
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Dune"}]),
        say("Anything else?"),
    )
    payload = main.ChatIn(message="could you keep Dune for me for later?", client_message_id=uuid.uuid4())

    async def send():
        response = await main.chat_stream(payload, SimpleNamespace(id=convo.user_id), _request())
        return await _events(response)

    first, second = await asyncio.gather(send(), send(), return_exceptions=True)
    from fastapi import HTTPException

    from app.database import Message

    refused = [r for r in (first, second) if isinstance(r, HTTPException)]
    answered = [r for r in (first, second) if not isinstance(r, BaseException)]
    assert len(answered) == 1 and len(refused) == 1, (first, second)
    assert answered[0][-1]["event"] == "done"
    for problem in refused:
        assert problem.status_code == 409 and problem.headers["X-Problem-Code"] == "already_answering"

    assert convo.watchlist() == ["Dune"]
    with convo.sessions() as db:
        sent = db.scalars(
            select(Message).where(Message.user_id == convo.user_id, Message.role == "user")
        ).all()
    assert len(sent) == 1
    assert len(model.requests) == 2  # one turn (tool round + answer), not two turns
    # Once answered, a resend replays the saved answer without running the turn again.
    replay = await send()
    assert replay[-1]["event"] == "done" and len(model.requests) == 2


class _FakeGroq:
    """Stands in for GroqProvider inside a real ModelChain."""

    plan: dict = {}
    calls: list = []

    def __init__(self, model, max_retries=2):
        self.model_name = model
        self.client = object()
        self.last_usage = {}

    async def stream_events(self, request):
        _FakeGroq.calls.append(self.model_name)
        outcome = _FakeGroq.plan.get(self.model_name)
        if isinstance(outcome, BaseException):
            raise outcome
        yield StreamEvent("text", text=f"Answer from {self.model_name}.")
        yield StreamEvent("finish", finish_reason="stop")


class _Status(Exception):
    def __init__(self, status, message="error", retry_after="5"):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, headers={"retry-after": retry_after})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_Status(429, "rate limited", retry_after="60")])
async def test_a_rate_limited_or_overloaded_model_hands_the_turn_to_the_next_model(world, failure):
    """End to end in model mode: the real chain moves on before any text, and the turn says which model answered.

    The limit here lasts a minute, longer than a turn waits for the first model.

    (A timeout or connection error rests the whole provider instead, see chain.failure_scope;
    that turn goes to the backup model, tests/test_model_fallback.py.)"""
    from app.inference.providers import chain as chain_module
    from app.services.provider_health import recent_turns

    convo, _, _, monkeypatch = world
    monkeypatch.setattr(chain_module, "GroqProvider", _FakeGroq)
    chain_module._COOLDOWN_UNTIL.clear()
    _FakeGroq.plan, _FakeGroq.calls = {"openai/gpt-oss-20b": failure}, []
    install_model(monkeypatch, chain_module.ModelChain(["openai/gpt-oss-20b", "openai/gpt-oss-120b"]))
    try:
        answer = await convo.say("explain what a sonnet is in one line")
        assert answer.error is None
        assert answer.text == "Answer from openai/gpt-oss-120b."
        assert _FakeGroq.calls[:2] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
        record = recent_turns()["latest"][0]
        assert record["model"] == "openai/gpt-oss-120b"
        # The failed model rests, so the next turn goes straight to the healthy one.
        _FakeGroq.calls = []
        await convo.say("and a haiku?")
        assert _FakeGroq.calls == ["openai/gpt-oss-120b"]
    finally:
        chain_module._COOLDOWN_UNTIL.clear()


@pytest.mark.asyncio
async def test_editing_deleting_and_turning_off_memory_change_what_the_model_is_told(world):
    from fastapi import HTTPException

    from app.database import Memory
    from app.memory import MemoryService
    from app.routers import auth

    convo, model, factory, monkeypatch = world
    monkeypatch.setattr(auth, "SessionLocal", factory)
    user = SimpleNamespace(id=convo.user_id)
    with factory() as db:
        MemoryService(db).save_memory(convo.user_id, "Watches films with their grandmother.", "fact", 0.7)
        memory_id = db.scalar(select(Memory.id).where(Memory.user_id == convo.user_id))

    async def told(message):
        model.script(say("ok"))
        await convo.say(message)
        return system_text(model.requests[-1])

    assert "grandmother" in await told("something for tonight?")
    auth.update_memory(
        memory_id, auth.MemoryUpdateRequest(content="Watches films with their aunt."), _request(), user
    )
    text = await told("something for tonight?")
    assert "aunt" in text and "grandmother" not in text
    auth.delete_memory(memory_id, _request(), user)
    assert "aunt" not in await told("something for tonight?")
    with pytest.raises(HTTPException):
        auth.delete_memory(memory_id, _request(), SimpleNamespace(id=convo.user_id + 999))

    with factory() as db:
        MemoryService(db).save_memory(convo.user_id, "Dislikes jump scares.", "fact", 0.7)
    assert "jump scares" in await told("a scary film?")
    auth.update_personalization_settings(
        auth.PersonalizationUpdateRequest(memory_enabled=False), _request(), user
    )
    text = await told("a scary film?")
    assert "jump scares" not in text and "turned memory off" in text


@pytest.mark.asyncio
async def test_a_guest_duplicate_send_acts_once(monkeypatch):
    import httpx

    from app import main
    from app.agent.actions import _guest_list

    install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    model.script(
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Dune"}]),
        say("Anything else?"),
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Dune"}]),
        say("Anything else?"),
    )
    transport = httpx.ASGITransport(app=main.app, client=("10.9.9.9", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session = (await client.post("/api/v1/guest/session")).json()["guest_session_id"]
        body = {"message": "could you keep Dune for me for later?", "client_message_id": str(uuid.uuid4())}
        first, second = await asyncio.gather(
            client.post("/api/v1/chat/stream", json=body), client.post("/api/v1/chat/stream", json=body)
        )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], statuses
    refused = first if first.status_code == 409 else second
    assert refused.json()["code"] == "already_answering"
    assert [item["movie_title"] for item in await _guest_list(session)] == ["Dune"]
    assert len(model.requests) == 2

    # After an error the same message can be retried at once (the claim is released).
    from tests.model_harness import say as _say

    model.script(ConnectionError("down"))
    retry = {"message": "write a two-line poem about rain", "client_message_id": str(uuid.uuid4())}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=client.cookies
    ) as again:
        await again.post("/api/v1/chat/stream", json=retry)
        model.script(_say("Rain on the roof."))
        second_try = await again.post("/api/v1/chat/stream", json=retry)
    assert second_try.status_code == 200


class _FailsOnce(_FakeGroq):
    failed: set = set()

    async def stream_events(self, request):
        _FakeGroq.calls.append(self.model_name)
        outcome = _FakeGroq.plan.get(self.model_name)
        if isinstance(outcome, BaseException) and self.model_name not in _FailsOnce.failed:
            _FailsOnce.failed.add(self.model_name)
            raise outcome
        yield StreamEvent("text", text=f"Answer from {self.model_name}.")
        yield StreamEvent("finish", finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [_Status(429, "rate limited", retry_after="30"), _Status(503, "over capacity")]
)
async def test_the_chat_model_is_waited_for_when_busy_never_replaced(world, failure):
    """Chat turns have one model (owner, 2026-10-16). A limit that lifts within the turn
    is waited out, the user is told it is busy, and the same model answers."""
    from app.inference.providers import chain as chain_module

    convo, _, _, monkeypatch = world
    slept: list[float] = []

    async def no_wait(seconds):
        slept.append(seconds)
        chain_module._COOLDOWN_UNTIL.clear()  # the limit has lifted

    monkeypatch.setattr(chain_module, "GroqProvider", _FailsOnce)
    monkeypatch.setattr(chain_module, "_pause", no_wait)
    chain_module._COOLDOWN_UNTIL.clear()
    _FailsOnce.failed = set()
    _FakeGroq.plan, _FakeGroq.calls = {"openai/gpt-oss-120b": failure}, []
    install_model(monkeypatch, chain_module.ModelChain(["openai/gpt-oss-120b"]))
    try:
        answer = await convo.say("explain what a sonnet is in one line")
        assert answer.error is None
        assert answer.text == "Answer from openai/gpt-oss-120b."
        assert _FakeGroq.calls == ["openai/gpt-oss-120b", "openai/gpt-oss-120b"]
        assert len(slept) == 1 and 0 < slept[0] <= 31
        statuses = [e for e in answer.events if e["event"] == "status"]
        assert any("busy" in json.dumps(e) for e in statuses)
    finally:
        chain_module._COOLDOWN_UNTIL.clear()
