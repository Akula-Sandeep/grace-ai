"""Reliability of the turn itself: time, failures, repeats, branches and what the model may say.

Each test reproduces a failure seen in the core before it was rebuilt:
a hanging model turned every turn into a timeout; TMDB being unreachable gave
a vague error and made every request wait out the same timeouts; "Try again"
or regenerate repeated side effects; context lived in a 6-hour cache that
ignored conversation branches; nothing checked what the model wrote.
"""

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from app.inference.models import StreamEvent
from tests.jarvis_harness import Conversation, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say, system_text


class Hanging(ScriptedModel):
    """A model that accepts the request and never answers (bounded as the provider chain does)."""

    async def _raw(self):
        await asyncio.sleep(100)
        yield StreamEvent("text", text="late")

    async def stream_events(self, request):
        from app.core import deadline

        self.requests.append(request)
        async for event in deadline.bounded_stream(
            self._raw(), first_item_seconds=deadline.FIRST_TOKEN_SECONDS
        ):
            yield event


class Stalls(ScriptedModel):
    """A model that writes a few words, then stops responding."""

    def __init__(self, text, *, stall_after):
        super().__init__()
        self.text, self.stall_after = text, stall_after

    async def _raw(self):
        for index, word in enumerate(self.text.split(" ")):
            if index >= self.stall_after:
                await asyncio.sleep(100)
            yield StreamEvent("text", text=word + " ")

    async def stream_events(self, request):
        from app.core import deadline

        self.requests.append(request)
        async for event in deadline.bounded_stream(
            self._raw(), first_item_seconds=deadline.FIRST_TOKEN_SECONDS
        ):
            yield event


@pytest.mark.asyncio
async def test_a_hanging_model_never_turns_a_turn_into_a_timeout_error(monkeypatch):
    """It ends well inside the deadline as a named, retryable outage."""
    from app.config import settings
    from app.core import deadline

    factory, _ = install(monkeypatch, offline=False)
    monkeypatch.setattr(settings, "agent_deadline_seconds", 9.0)
    monkeypatch.setattr(deadline, "FIRST_TOKEN_SECONDS", 2.0)
    install_model(monkeypatch, Hanging())
    convo = Conversation(factory, new_user(factory))

    started = time.monotonic()
    answer = await convo.say("Recommend a sci-fi film under two hours")
    assert answer.error is not None and answer.error["code"] != "timeout", answer.events
    assert answer.error["retryable"] is True
    assert time.monotonic() - started < 9.0


@pytest.mark.asyncio
async def test_a_model_that_stalls_mid_answer_keeps_what_was_said(monkeypatch):
    from app.config import settings
    from app.core import deadline

    factory, _ = install(monkeypatch, offline=False)
    monkeypatch.setattr(settings, "agent_deadline_seconds", 8.0)
    monkeypatch.setattr(deadline, "FIRST_TOKEN_SECONDS", 2.0)
    install_model(
        monkeypatch,
        Stalls("Arrival is a thoughtful first-contact film about language and time.", stall_after=4),
    )
    convo = Conversation(factory, new_user(factory))
    answer = await convo.say("What do you think of Arrival as a film? Share your opinion")
    assert answer.error is None, answer.events
    assert answer.text.startswith("Arrival is a thoughtful")
    assert "may be incomplete" in answer.text
    assert answer.events[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_tmdb_unreachable_says_so_and_fails_fast_after_the_first_failures(monkeypatch):
    from app.services.upstream_errors import UpstreamUnavailable

    factory, tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    calls = {"n": 0}

    async def unreachable(*_args, **_kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        raise UpstreamUnavailable("TMDB could not be reached.", provider="tmdb")

    for name in ("_get", "search_movies", "discover_movies", "get_movie_details", "get_recommendations"):
        monkeypatch.setattr(tmdb, name, unreachable)
    convo = Conversation(factory, new_user(factory))
    model.script(
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Dune"}]),
        lambda request: say(last_result(request)["error"]),
    )
    first = await convo.say("add Dune to my watchlist")
    # The model is told which service failed, so it can say so; nothing was saved.
    assert "the movie database (TMDB) could not be reached; nothing was changed." in first.text
    assert convo.watchlist() == []


@pytest.mark.asyncio
async def test_the_tmdb_breaker_opens_and_recovers():
    from app.services.resilience import breaker

    circuit = breaker("tmdb-test", threshold=2, network_rest=0.2)
    assert circuit.allow()
    circuit.failure("connect_error")
    assert circuit.allow()  # one failure is not an outage
    circuit.failure("connect_error")
    assert not circuit.allow()  # two in a row: fail fast
    assert circuit.snapshot()["state"] == "open"
    await asyncio.sleep(0.25)
    assert circuit.allow()  # rest over: calls may go out again
    circuit.success()
    assert circuit.snapshot()["state"] == "closed"
    circuit.failure("not_found")
    assert circuit.allow()  # "not found" is a successful call


@pytest.fixture
def scripted(monkeypatch):
    factory, _ = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model


def _act(action, title, **extra):
    return [call("update_my_list", action=action, movies=[{"title": title}], **extra), say("")]


@pytest.mark.asyncio
async def test_regenerating_an_action_never_repeats_it(scripted):
    convo, model = scripted
    model.script(*(_act("remind", "Dune", when="tomorrow at 8pm") * 2))
    first = await convo.say("remind me to watch Dune tomorrow at 8pm")
    assert "I'll remind you on" in first.text
    original = convo.user_message_ids()[-1]
    again = await convo.say("remind me to watch Dune tomorrow at 8pm", regenerate_of=uuid.UUID(original))
    assert "I'll remind you on" in again.text  # the same outcome, reported again
    assert len(convo.reminders()) == 1  # ...but only one reminder exists

    model.script(
        *_act("add_to_watchlist", "Arrival"),
        *_act("remove_from_watchlist", "Arrival"),
        *_act("remove_from_watchlist", "Arrival"),
    )
    await convo.say("add Arrival to my watchlist")
    removed = await convo.say("remove Arrival from my watchlist")
    assert removed.text.startswith("Removed **Arrival**")
    regenerated = await convo.say(
        "remove Arrival from my watchlist", regenerate_of=uuid.UUID(convo.user_message_ids()[-1])
    )
    assert regenerated.text.startswith("Removed **Arrival**")  # not "wasn't on your watchlist"


@pytest.mark.asyncio
async def test_context_follows_the_branch_when_a_message_is_edited(scripted):
    """Editing a message restarts from the state before it, not from the latest turn."""
    convo, model = scripted
    model.script(call("movie_details", title="Dune"), say("**Dune** (2021)."))
    await convo.say("Tell me about Dune")
    model.script(call("movie_details", title="Arrival"), say("**Arrival** (2016)."))
    await convo.say("Tell me about Arrival")
    model.script(say("Denis Villeneuve."))
    await convo.say("Who directed it?")
    assert "Film in focus" in system_text(model.requests[-1]) and "**Arrival**" in system_text(
        model.requests[-1]
    )
    edited_id = convo.user_message_ids()[1]  # "Tell me about Arrival"
    model.script(say("Denis Villeneuve."))
    await convo.say("Who directed it?", edit_message_id=uuid.UUID(edited_id))
    focus = next(line for line in system_text(model.requests[-1]).splitlines() if "Film in focus" in line)
    assert "**Dune**" in focus  # the branch before Arrival was discussed


@pytest.mark.asyncio
async def test_context_survives_losing_the_session_cache(scripted):
    """Working state is saved with each reply, so a cache restart loses nothing."""
    from app.shared_state import _InMemoryBackend, shared_state

    convo, model = scripted
    model.script(call("recommend_movies", genres=["Science Fiction"]), say("Try **Arrival**."))
    await convo.say("Recommend a sci-fi film")
    shared_state._backend = _InMemoryBackend()  # Redis restarted, or another worker answers
    model.script(say("Here's a shorter one."))
    await convo.say("shorter")
    state = system_text(model.requests[-1])
    assert "Recommendation request still in force" in state and "Science Fiction" in state


@pytest.mark.asyncio
async def test_each_turn_is_recorded_for_settings_help(scripted):
    from app.services.provider_health import recent_turns

    convo, model = scripted
    model.script(say("Try **Arrival**."), *_act("add_to_watchlist", "Arrival"))
    await convo.say("Recommend a sci-fi film")
    await convo.say("add the first one")
    recent = recent_turns()
    assert recent["count"] == 2
    latest = recent["latest"][0]
    assert latest["intent_source"] == "model" and latest["handler"] == "model"


@pytest.mark.asyncio
async def test_non_streaming_chat_never_returns_control_events(monkeypatch):
    from app import main

    factory, _ = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    model.script(*_act("add_to_watchlist", "Dune"))
    user_id = new_user(factory)
    result = await main.chat(
        main.ChatIn(message="add Dune to my watchlist"), SimpleNamespace(id=user_id), _plain_request()
    )
    assert "event:" not in result.response
    assert result.response.startswith("Added **Dune**")


def _plain_request():
    from tests.jarvis_harness import _request

    return _request()


def test_branch_lineage_is_one_bounded_query(monkeypatch):
    """Long conversations must not make every turn slower (one query per ancestor before)."""
    import os

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base, Message, User, branch_lineage_ids
    from app.database import Conversation as ConversationRow

    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    engine = (
        create_engine(url)
        if url
        else create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username=f"lineage-{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@x.test",
            password_hash="x",
        )
        db.add(user)
        db.flush()
        conversation = ConversationRow(user_id=user.id)
        db.add(conversation)
        db.flush()
        parent = None
        for index in range(120):
            message = Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"m{index}",
                parent_id=parent,
            )
            db.add(message)
            db.flush()
            parent = message.uuid_id
        db.commit()
        queries = []
        event.listen(engine, "before_cursor_execute", lambda *args: queries.append(args[2]))
        ids = branch_lineage_ids(db, conversation.id, parent, max_depth=20)
        assert len(ids) == 20 and len(queries) == 1
    engine.dispose()
