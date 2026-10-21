"""When the chat model can't answer: an honest, retryable outage, never another model's answer.

Chat turns are answered by one model, the strongest, and wait for it when it is busy
(owner, 2026-10-16; the waiting is in ``inference/providers/chain.py``, tested in
``test_model_reliability.py``). Real endpoint, dispatcher and model loop; the model and
TMDB are faked. These prove the plumbing, not the quality of an answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.jarvis_harness import Conversation, install, new_user
from tests.model_harness import ScriptedModel, call, install_model


@pytest.fixture
def world(monkeypatch):
    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return SimpleNamespace(model=model, convo=Conversation(factory, new_user(factory)))


class _RateLimited(RuntimeError):
    status_code = 429


def rate_limited(model, *, after=()):
    model.script(*after, *[_RateLimited("Rate limit reached on tokens per minute.")] * 10)


@pytest.mark.asyncio
async def test_with_the_chat_model_at_its_limit_the_turn_is_a_retryable_outage(world):
    rate_limited(world.model)
    reply = await world.convo.say("what is film noir?")
    assert reply.error is not None
    assert reply.error.get("code") == "rate_limited"
    assert "film noir is" not in reply.text.casefold()


@pytest.mark.asyncio
async def test_a_limit_after_the_tools_ran_is_still_an_outage_not_a_stand_in_answer(world):
    rate_limited(world.model, after=[call("movie_details", title="Interstellar")])
    reply = await world.convo.say("how long is interstellar?")
    assert reply.error is not None
    assert "169" not in reply.text


@pytest.mark.asyncio
async def test_a_change_made_before_the_model_ran_out_is_confirmed_not_reported_as_an_outage(world):
    """Live run 3 (L01): "yes" added the film, then the model hit its limit before writing.
    The change is real and read back, so the app's confirmation is the reply."""
    rate_limited(
        world.model,
        after=[call("update_my_list", action="add_to_watchlist", movies=[{"title": "Interstellar"}])],
    )
    reply = await world.convo.say("add interstellar to my watchlist")
    assert reply.error is None
    assert "usage limit" not in reply.text
    cards = [e["text"] for e in reply.events if e["event"] == "action"]
    assert len(cards) == 1 and cards[0] in reply.text
    assert world.convo.watchlist() == ["Interstellar"]
