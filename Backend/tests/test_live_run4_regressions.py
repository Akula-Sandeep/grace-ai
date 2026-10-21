"""Defects found by the fourth live evaluation (owner's machine, 2026-10-14 12:41 IST).

Report: eval-reports/20261014-124158/{model,classic}. Each test reproduces one
defect offline (a scripted model, a fake catalogue or a fake provider); none of
this is a live check.
"""

from __future__ import annotations

import pytest

from tests.jarvis_harness import Conversation, install, new_user
from tests.model_harness import ScriptedModel, install_model

# --------------------------------------------------------------------------- C01, classic mode: still out of reach

C01 = [
    "For context: my name is Priya, I only watch dubbed Hindi versions, and I watch with my grandmother.",
    "What causes rain?",
    "Who wrote Hamlet?",
    "What is photosynthesis?",
    "Define entropy briefly.",
    "What's a prime number?",
    "Given what I told you at the start, what kind of version of a film should I look for, and who will I watch it with?",
]


# --------------------------------------------------------------------------- R02, classic mode: the router dropped "(2021)"


# --------------------------------------------------------------------------- R03, classic mode: a digest with no recorded cause


class _Limited(RuntimeError):
    status_code = 429


class _LimitedProvider:
    model_name = "openai/gpt-oss-120b"
    provider_name = "groq"

    async def stream(self, _request):
        raise _Limited("rate limited")
        yield ""  # pragma: no cover


class _LimitedRouter:
    def fast(self):
        return _LimitedProvider()

    def deep(self):
        return _LimitedProvider()


DUNE_1984 = {
    "status": "found",
    "details": {
        "id": 841,
        "title": "Dune",
        "release_date": "1984-12-14",
        "runtime": 137,
        "genres": [{"name": "Science Fiction"}],
        "overview": "In the year 10,191, the spice is vital to space travel.",
    },
}


# --------------------------------------------------------------------------- P02, model mode: "Let me check…" and then nothing


class _RateLimited(RuntimeError):
    status_code = 429


@pytest.fixture
def scripted(monkeypatch):
    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model


def _preamble_then_tool(text, tool, **arguments):
    from app.inference.models import StreamEvent
    from tests.model_harness import calls

    return [StreamEvent("text", text=text), *calls((tool, arguments))]


@pytest.mark.asyncio
async def test_a_limit_after_the_preamble_says_the_answer_is_not_finished(scripted):
    """No other model writes the rest: the user keeps what was said and is told plainly."""
    convo, model = scripted
    model.script(
        _preamble_then_tool("Let me look that up. ", "movie_details", title="Arrival"),
        _RateLimited("rate limited"),
    )
    answer = await convo.say("how long is Arrival?")
    assert answer.text.startswith("Let me look that up.")
    assert "usage limit" in answer.text
    assert "116" not in answer.text


@pytest.mark.asyncio
async def test_a_failure_in_the_middle_of_the_answer_itself_still_says_incomplete(scripted):
    from app.inference.models import StreamEvent

    convo, model = scripted
    model.script([StreamEvent("text", text="Arrival is about language and time. "), _RateLimited("x")])
    answer = await convo.say("what do you think of Arrival?")
    assert answer.text.startswith("Arrival is about language and time.")
    assert "may be incomplete" in answer.text


# --------------------------------------------------------------------------- prompt rules both paths share


# --------------------------------------------------------------------------- R01, classic mode: a list written without tools


def test_a_chat_answer_naming_one_film_does_not_replace_the_list():
    from app.agent import working_state as working

    state = working.empty_state()
    working.start_turn(state)
    working.record_references(state, [{"title": "A"}, {"title": "B"}])
    working.order_by_answer(state, "- **A** (2001)\n- **B** (2002)")
    working.start_turn(state)
    working.order_by_answer(state, "You might also like **C** (2003).")
    working.start_turn(state)
    assert [m["title"] for m in state["last_answer_movies"]] == ["A", "B"]


# --------------------------------------------------------------------------- F02, classic mode: why no streaming services?


@pytest.mark.asyncio
async def test_a_limit_after_the_preamble_says_there_is_no_answer_yet(scripted, monkeypatch):
    convo, model = scripted
    model.script(
        _preamble_then_tool("Let me check the runtimes. ", "person_details", name="Denis Villeneuve"),
        _RateLimited("rate limited"),
    )
    answer = await convo.say("Who directed Arrival, and which of their other films is the shortest?")
    assert answer.text.startswith("Let me check the runtimes.")
    assert "reached its usage limit before it could finish" in answer.text
    assert "may be incomplete" not in answer.text  # live: "…so this answer may be incomplete."
