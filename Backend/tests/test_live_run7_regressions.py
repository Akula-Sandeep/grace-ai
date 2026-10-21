"""Defects found by the seventh live evaluation (owner's machine, 2026-10-15 10:35 IST).

Report: eval-reports/20261015-103513/{model,classic}. Each test reproduces one
defect offline (a scripted model, a fake catalogue or a fake provider); none of
this is a live check.
"""

from __future__ import annotations

import pytest

from tests.jarvis_harness import Conversation, film, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say


@pytest.fixture
def world(monkeypatch):
    factory, tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model, tmdb


# --------------------------------------------------------------------------- F01 classic: "Thanks, that's useful to know"


# --------------------------------------------------------------------------- M01 model: "not tagged feel-good"


@pytest.mark.asyncio
async def test_a_mood_passed_as_a_genre_still_finds_films(world):
    convo, model, _ = world
    seen = {}

    def answer(request):
        seen.update(last_result(request))
        return say("Here are some.")

    model.script(call("recommend_movies", genres=["feel-good"]), answer)
    await convo.say("Recommend a feel-good movie.")
    assert seen["strict_matches"], seen  # live: the answer said no film is "tagged feel-good"
    assert "no film is tagged with a mood word" in seen["note"]
    assert not seen.get("unrecognized_genres")


# --------------------------------------------------------------------------- F02 model: "not listed for the Netherlands"


@pytest.mark.asyncio
async def test_where_to_watch_data_comes_when_the_model_asks_for_it(world):
    """The model decides to look availability up (where_to_watch); no keyword rule adds it."""
    convo, model, tmdb = world
    tmdb.catalog[496243] = film(
        496243,
        "Parasite",
        133,
        year=2019,
        genres=(35, 53, 18),
        votes=21000,
        popularity=80,
        director="Bong Joon Ho",
    )
    seen = {}

    def answer(request):
        seen.update(last_result(request))
        return say("It streams on Example+.")

    model.script(call("movie_details", title="Parasite", where_to_watch=True), answer)
    await convo.say("Where can I stream Parasite?")
    movie = seen["movie"]
    assert movie["where_to_watch"] == {"stream": ["Example+"]}
    assert "dubbed audio" in movie["about_this_data"]
    model.script(call("movie_details", title="Parasite"), answer)
    await convo.say("Who directed Parasite? Where can I stream it?")
    assert "where_to_watch" not in seen["movie"]


# --------------------------------------------------------------------------- P02 model: a filmography answered from memory


# --------------------------------------------------------------------------- classic: orphan lines, 【1】, ratings, prompt


@pytest.mark.asyncio
async def test_a_film_lookup_carries_its_rating(monkeypatch):
    from app.agent.router import QueryClassification
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    _factory, tmdb = install(monkeypatch, offline=True)
    engine = MovieDataFallbackEngine(tmdb=tmdb)
    request = QueryClassification(
        domain="movie", intent="metadata", needs_live_search=False, movie_title="Arrival"
    )
    result = await engine.lookup_query(request, "Tell me about Arrival")
    assert result["movie"]["vote_average"] == 7.6 and result["movie"]["vote_count"] == 18000


def test_the_prompt_keeps_whole_edits_and_unasked_availability_out():
    from app.core.prompts import GRACE_SYSTEM_PROMPT

    assert "give the whole updated version, changed only where asked" in GRACE_SYSTEM_PROMPT
    assert "leave availability out entirely" in GRACE_SYSTEM_PROMPT
