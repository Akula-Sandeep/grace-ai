"""The action pipeline (agent/actions.py) driven by the model's tool calls, end to end.

Real endpoint, database and ledger; TMDB and the model are faked.
"""

from __future__ import annotations

import pytest

from app.agent import working_state as working
from app.agent.actions import _stored_film, run_action
from app.agent.router import QueryClassification
from tests.jarvis_harness import Conversation, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say


@pytest.fixture
def world(monkeypatch):
    factory, tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model, tmdb, factory


@pytest.mark.asyncio
async def test_a_reminder_with_no_film_is_set_and_a_second_one_after_tool_results_is_refused(world):
    """Injected text could ask for a reminder; after tool results a bare one is never set."""
    convo, model, _tmdb, _factory = world
    results = []

    def again(request):
        results.append(last_result(request))
        return call("update_my_list", action="remind", when="friday at 9pm", note="spam")

    def answer(request):
        results.append(last_result(request))
        return say("Done.")

    model.script(
        call("update_my_list", action="remind", when="tomorrow at 8pm", note="pick a film"), again, answer
    )
    reply = await convo.say("remind me tomorrow at 8pm to pick a film")
    assert reply.error is None
    assert results[0]["outcome"].startswith("I'll remind you on")
    assert results[1]["status"] == "not_done"
    assert convo.reminders() == ["Pick a film"]


@pytest.mark.asyncio
async def test_without_targets_the_pipeline_asks_and_guests_cannot_keep_ratings():
    state = working.empty_state()
    rate = QueryClassification(domain="movie", intent="rate_movie", needs_live_search=False, rating=8)
    guest = [chunk async for chunk in run_action(rate, "rate it 8", state=state, guest_session_id="g")]
    assert guest == ["Sign in so I can save your ratings."]
    nobody = [chunk async for chunk in run_action(rate, "rate it 8", state=state)]
    assert nobody == ["Sign in so I can save your ratings."]

    class Engine:
        closed = False

        async def close(self):
            Engine.closed = True

    add = QueryClassification(domain="movie", intent="add_to_watchlist", needs_live_search=False)
    asked = [
        chunk
        async for chunk in run_action(add, "add it", state=state, guest_session_id="g", movie_engine=Engine())
    ]
    assert asked == ["Which film do you mean?"]


@pytest.mark.asyncio
async def test_a_stored_film_is_read_back_from_the_database(world):
    convo, model, _tmdb, factory = world
    model.script(
        call("update_my_list", action="add_to_watchlist", movies=[{"title": "Arrival"}]), say("Added.")
    )
    await convo.say("add arrival to my watchlist")
    stored = await _stored_film(329865, factory)
    assert stored["title"] == "Arrival" and stored["id"] == 329865
    assert await _stored_film(999999999, factory) is None
    assert await _stored_film(329865, None) is None
