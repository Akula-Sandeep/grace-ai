"""MOVIE-002: titles typed in another language or script, and honesty about dubbed versions.

TMDB's search matches original and alternative titles in any script but returns
the English title (Grace searches with language=en-US). These tests use a
catalog entry with a Korean original title and a Hindi alternative title, and a
search that behaves like TMDB's in that respect. The model is scripted; the live
version of this check is scenario F03 and F05 in app/evals/live_scenarios.json.
"""

from __future__ import annotations

import pytest

from tests.jarvis_harness import CATALOG, Conversation, _norm, film, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say, tool_results

PARASITE = {
    **film(
        496243,
        "Parasite",
        133,
        year=2019,
        genres=(35, 53, 18),
        director="Bong Joon Ho",
        rating=8.5,
        votes=18000,
        popularity=90.0,
    ),
    "original_title": "기생충",
    "original_language": "ko",
    "translations": {
        "translations": [
            {"iso_639_1": "hi", "data": {"title": "पैरासाइट", "overview": "एक गरीब परिवार..."}},
            {"iso_639_1": "ko", "data": {"title": "기생충", "overview": "전원 백수로 살 길 막막하지만..."}},
        ]
    },
}
ALTERNATIVE_TITLES = {496243: ["पैरासाइट", "Gisaengchung"]}


@pytest.fixture
def world(monkeypatch):
    factory, tmdb = install(monkeypatch, offline=False)
    tmdb.catalog[496243] = PARASITE

    def search(query):
        typed = " ".join(query.casefold().split())
        words = [w for w in _norm(query) if w not in {"the", "a", "an"}]
        hits = []
        for movie in tmdb.catalog.values():
            names = [movie["title"], movie["original_title"], *ALTERNATIVE_TITLES.get(movie["id"], [])]
            if any(typed == " ".join(n.casefold().split()) for n in names) or (
                words and all(w in _norm(movie["title"]) for w in words)
            ):
                hits.append(movie)
        return {"results": [tmdb._list_item(m) for m in hits]}

    monkeypatch.setattr(tmdb, "_search", search)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model


def test_the_catalog_fixture_does_not_leak_into_other_tests():
    assert all(movie["id"] != 496243 for movie in CATALOG)


@pytest.mark.asyncio
async def test_a_korean_original_title_resolves_to_the_film(world):
    convo, model = world
    model.script(
        call("movie_details", title="기생충", language="ko"),
        lambda r: say("감독은 " + ", ".join(last_result(r)["movie"]["directors"]) + "입니다."),
    )
    answer = await convo.say("기생충 감독이 누구야?")
    movie = tool_results(model.requests[-1])[0]["movie"]
    assert movie["tmdb_id"] == 496243 and movie["title"] == "Parasite"
    assert movie["localized_title"] == "기생충"
    assert "Bong Joon Ho" in answer.text


@pytest.mark.asyncio
async def test_a_title_in_another_script_offers_the_candidate_by_id_and_the_model_can_pick_it(world):
    convo, model = world
    model.script(
        call("movie_details", title="पैरासाइट"),
        lambda r: call("movie_details", tmdb_id=last_result(r)["candidates"][0]["tmdb_id"], language="hi"),
        lambda r: say(last_result(r)["movie"]["localized_title"] + " — " + last_result(r)["movie"]["title"]),
    )
    answer = await convo.say("पैरासाइट फिल्म के बारे में बताओ")
    first = tool_results(model.requests[1])[0]
    assert first["status"] == "ambiguous"
    assert first["candidates"][0] == {
        "tmdb_id": 496243,
        "title": "Parasite",
        "original_title": "기생충",
        "year": 2019,
    }
    assert answer.text.startswith("पैरासाइट — Parasite")


@pytest.mark.asyncio
async def test_availability_says_the_data_has_no_dub_or_subtitle_information(world):
    """F05: nothing in the tool result lets the model claim a Hindi dub exists."""
    convo, model = world
    model.script(
        call("movie_details", title="Parasite", where_to_watch=True),
        say("I can see where it streams, but not which audio versions each service has."),
    )
    await convo.say("Is there a Hindi dubbed version of Parasite on any streaming service?")
    movie = tool_results(model.requests[-1])[0]["movie"]
    assert "where_to_watch" in movie
    assert "dubbed audio" in movie["about_this_data"]
    assert "hindi" not in str(movie.get("where_to_watch")).casefold()
