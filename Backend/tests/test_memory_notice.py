"""A taste the user states is saved by the model's remember_taste tool, announced, counted once.

Scripted models prove the plumbing (storage, the "Memory updated" event, Undo, the
memory switch, the taint rule); whether a real model calls the tool at the right
moments is the live evaluation's to show.
"""

import pytest

from app import main
from app.database import Preference
from tests.jarvis_harness import Conversation, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say

LOVES_SCIFI_HATES_HORROR = [
    {"kind": "genre", "value": "science fiction", "feeling": "likes", "strong": True},
    {"kind": "genre", "value": "horror", "feeling": "dislikes", "strong": True},
]


def _memory(reply):
    return [e for e in reply.events if e["event"] == "memory"]


def _prefs(convo):
    with convo.sessions() as db:
        return sorted(
            (p.polarity, p.value, p.evidence_count)
            for p in db.query(Preference).filter_by(user_id=convo.user_id).all()
        )


@pytest.fixture
def world(monkeypatch):
    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return factory, model, monkeypatch


@pytest.mark.asyncio
async def test_a_stated_taste_is_saved_announced_and_never_double_counted(world):
    factory, model, monkeypatch = world
    convo = Conversation(factory, new_user(factory))
    model.script(call("remember_taste", items=LOVES_SCIFI_HATES_HORROR), say("Got it."))
    reply = await convo.say("I love sci-fi but I hate horror")
    assert reply.text == "Got it."
    events = _memory(reply)
    assert len(events) == 1
    labels = sorted(item["label"] for item in events[0]["items"])
    assert labels == ["Avoids Horror", "Likes Science Fiction"]
    assert all(isinstance(item["id"], int) and item["kind"] == "preference" for item in events[0]["items"])
    kinds = [e["event"] for e in reply.events]
    assert kinds.index("memory") < kinds.index("token")  # announced before the reply text
    saved = _prefs(convo)
    assert len(saved) == 2 and all(count == 1 for *_rest, count in saved)

    # The after-response pass never re-parses the message into preferences.
    await main._ingest_chat_memory_after_response(convo.user_id, "I love sci-fi but I hate horror", None)
    assert _prefs(convo) == saved

    # Regenerating the same request: the model calls the tool again; the ledger reports the
    # same items and nothing is stored twice.
    model.script(call("remember_taste", items=LOVES_SCIFI_HATES_HORROR), say("Got it."))
    first_message = convo.user_message_ids()[0]
    again = await convo.say("I love sci-fi but I hate horror", regenerate_of=first_message)
    assert [e["items"] for e in _memory(again)] == [events[0]["items"]]
    assert _prefs(convo) == saved

    # Undo is the existing delete endpoint.
    from types import SimpleNamespace

    from app.routers import auth

    monkeypatch.setattr(auth, "SessionLocal", factory)
    for item in events[0]["items"]:
        auth.delete_preference(item["id"], SimpleNamespace(), user=SimpleNamespace(id=convo.user_id))
    assert _prefs(convo) == []


@pytest.mark.asyncio
async def test_a_newer_statement_replaces_the_opposite_older_one(world):
    factory, model, _ = world
    convo = Conversation(factory, new_user(factory))
    model.script(
        call("remember_taste", items=[{"kind": "trait", "value": "slow pacing", "feeling": "dislikes"}]),
        say("Noted."),
    )
    await convo.say("slow pacing kills a film for me")
    model.script(
        call("remember_taste", items=[{"kind": "trait", "value": "slow pacing", "feeling": "likes"}]),
        say("Noted."),
    )
    await convo.say("actually I've started enjoying slow films")
    assert [(p, v) for p, v, _n in _prefs(convo)] == [("prefer", "slow pacing")]


@pytest.mark.asyncio
async def test_nothing_is_stored_or_announced_with_memory_off(world):
    from app.database import UserPersonalizationSetting

    factory, model, _ = world
    user_id = new_user(factory)
    with factory() as db:
        db.add(UserPersonalizationSetting(user_id=user_id, memory_enabled=False))
        db.commit()
    convo = Conversation(factory, user_id)
    model.script(
        call("remember_taste", items=[{"kind": "genre", "value": "science fiction", "feeling": "likes"}]),
        say("Memory is off, so I won't keep that."),
    )
    reply = await convo.say("I love sci-fi")
    assert last_result(model.requests[-1])["status"] == "memory_off"
    assert _memory(reply) == [] and _prefs(convo) == []


@pytest.mark.asyncio
async def test_a_taste_found_in_a_tool_result_is_never_saved(world):
    """Only what the user says about themselves is stored, never what a page or review says."""
    factory, model, _ = world
    convo = Conversation(factory, new_user(factory))
    model.script(
        call("movie_details", title="Arrival"),
        call("remember_taste", items=[{"kind": "genre", "value": "horror", "feeling": "likes"}]),
        say("Here is Arrival."),
    )
    reply = await convo.say("tell me about arrival")
    assert last_result(model.requests[-1])["status"] == "not_done"
    assert _memory(reply) == [] and _prefs(convo) == []


@pytest.mark.asyncio
async def test_guests_tastes_are_not_saved():
    from app.agent import working_state as working
    from app.agent.model_tools import TurnTools

    tools = TurnTools(
        user_message="I love sci-fi",
        chat_history=[],
        state=working.empty_state(),
        router=None,
        guest_session_id="guest-1",
        movie_engine=object(),
    )
    out = [
        item
        async for item in tools.remember_taste(
            {"items": [{"kind": "genre", "value": "science fiction", "feeling": "likes"}]}
        )
    ]
    assert out == [("result", {"status": "needs_account", "note": "Guests' tastes are not saved."})]
