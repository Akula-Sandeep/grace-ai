"""Defects found by the sixth live evaluation (owner's machine, 2026-10-14 19:28 IST).

Report: eval-reports/20261014-192833/{model,classic}. Each test reproduces one
defect offline (a scripted model, a fake catalogue or a fake provider); none of
this is a live check.
"""

from __future__ import annotations

import pytest

from tests.jarvis_harness import Conversation, film, install, new_user
from tests.model_harness import ScriptedModel, install_model

PRIMES = [n for n in range(2, 5000) if all(n % d for d in range(2, int(n**0.5) + 1))]


def _parasite(tmdb):
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


@pytest.fixture
def world(monkeypatch):
    factory, tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model, tmdb


# --------------------------------------------------------------------------- C01, model: every prime up to 4999


# --------------------------------------------------------------------------- classic: [841] citations, <code> tags


# --------------------------------------------------------------------------- F02 and F05, classic: where to watch


# --------------------------------------------------------------------------- routing while every router model rests


# --------------------------------------------------------------------------- R03: "No, I meant the 1984 one."


# --------------------------------------------------------------------------- R03 classic: which Dune


@pytest.mark.asyncio
async def test_a_title_most_people_mean_is_chosen_and_the_others_are_named(monkeypatch):
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    _factory, tmdb = install(monkeypatch, offline=True)
    tmdb.catalog[841] = film(841, "Dune", 137, year=1984, genres=(28, 12, 878), votes=3500, popularity=30)
    tmdb.catalog[781732] = film(781732, "Dune", 9, year=2020, genres=(16,), votes=4, popularity=1)
    engine = MovieDataFallbackEngine(tmdb=tmdb)
    match = await engine.resolve_movie("Dune")
    assert match["status"] == "found" and match["details"]["id"] == 438631
    assert match["other_films_with_this_title"] == ["Dune (1984)"]  # not the 9-minute short


def test_an_ambiguous_title_offers_only_well_known_films():
    from app.services.movie_data_fallback import _well_known

    films = [
        {"id": 1, "vote_count": 7000},
        {"id": 2, "vote_count": 2600},
        {"id": 3, "vote_count": 3},
    ]
    assert [f["id"] for f in _well_known(films)] == [1, 2]


# --------------------------------------------------------------------------- C01 classic: "Who wrote Hamlet?"


def test_film_credits_include_the_work_it_adapts():
    from app.agent.model_tools import movie_card

    hamlet = film(
        10549, "Hamlet", 242, year=1996, genres=(18,), votes=600, popularity=10, director="Kenneth Branagh"
    )
    hamlet["credits"]["crew"].append(
        {"id": 1, "name": "William Shakespeare", "job": "Play", "department": "Writing"}
    )
    assert movie_card(hamlet)["based_on_work_by"] == ["William Shakespeare"]


# --------------------------------------------------------------------------- R02 classic: "the evidence was limited"


def test_comparison_guidance_does_not_ask_for_evidence_was_limited():
    from app.agent.composite import COMPARISON_GUIDANCE

    assert "say the evidence was limited instead" not in COMPARISON_GUIDANCE
    assert "do not say the evidence was limited" in COMPARISON_GUIDANCE
    assert "structured facts" in COMPARISON_GUIDANCE


# --------------------------------------------------------------------------- F06 classic: an English one-liner for a Hindi question


# --------------------------------------------------------------------------- M01 classic: "genres you said to avoid"


@pytest.mark.asyncio
async def test_no_saved_preferences_means_no_saved_preferences_note(monkeypatch):
    from app.agent.router import QueryClassification
    from app.services.recommendation_engine import recommend

    _factory, tmdb = install(monkeypatch, offline=True)
    request = QueryClassification(
        domain="movie", intent="recommendation", needs_live_search=False, mood_keywords=["feel-good"]
    )
    result = await recommend(request, tmdb=tmdb)
    assert "saved_preferences_applied" not in result  # its note made the reply invent an exclusion


# --------------------------------------------------------------------------- R01 and M01: what ranks first


def test_a_music_drama_ranks_below_a_real_thriller_when_thrillers_are_asked_for():
    from app.agent.router import QueryClassification
    from app.services.recommendation_engine import soft_score

    request = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        genres=["Thriller"],
        max_runtime_minutes=120,
    )
    whiplash = {"id": 1, "vote_average": 8.4, "vote_count": 15000, "genre_ids": [18, 10402, 53]}
    thriller = {"id": 2, "vote_average": 8.2, "vote_count": 14000, "genre_ids": [80, 53]}
    assert soft_score(thriller, request, reference_ids=set()) > soft_score(
        whiplash, request, reference_ids=set()
    )


def test_something_darker_favours_horror_and_crime_over_a_war_drama():
    from app.agent.router import QueryClassification
    from app.services.recommendation_engine import soft_score

    request = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        genres=["Thriller"],
        mood_keywords=["darker"],
    )
    war_drama = {"id": 1, "vote_average": 8.0, "vote_count": 17000, "genre_ids": [18, 36, 53, 10752]}
    horror = {"id": 2, "vote_average": 7.9, "vote_count": 9000, "genre_ids": [27, 53]}
    assert soft_score(horror, request, reference_ids=set()) > soft_score(
        war_drama, request, reference_ids=set()
    )


def test_a_high_rating_on_few_votes_does_not_outrank_an_established_film():
    from app.agent.router import QueryClassification
    from app.services.recommendation_engine import soft_score

    request = QueryClassification(
        domain="movie", intent="recommendation", needs_live_search=False, mood_keywords=["feel-good"]
    )
    new = {"id": 1, "vote_average": 9.1, "vote_count": 120, "genre_ids": [16, 12]}
    coco = {"id": 2, "vote_average": 8.2, "vote_count": 20000, "genre_ids": [16, 10751, 10402]}
    assert soft_score(coco, request, reference_ids=set()) > soft_score(new, request, reference_ids=set())


# --------------------------------------------------------------------------- R01 model: "streaming on" four rent-or-buy stores


def test_rent_or_buy_stores_are_not_called_streaming():
    from app.agent.model_tools import _providers_summary

    summary = _providers_summary(
        {
            "flatrate": [],
            "rent": [{"provider_name": "Pathé Thuis"}],
            "buy": [{"provider_name": "Apple TV Store"}],
        }
    )
    assert summary == {"rent": ["Pathé Thuis"], "buy": ["Apple TV Store"]}  # no "stream" key


# --------------------------------------------------------------------------- G04: invented places in a trip plan


PLAN = (
    "**Day 1:** Tour Cinecittà Studios, then visit the Museum of Cinema at the Palazzo della Civiltà del Lavoro.\n\n"
    "**Day 2:** Walk Trastevere and Via Margutta, where Roman Holiday was filmed.\n\n"
    "**Day 3:** See The Last Temptation of Christ filming set in the Vatican Museums."
)
FIXED = (
    "**Day 1:** Tour Cinecittà Studios, then the MIAC film museum on the Cinecittà lot.\n\n"
    "**Day 2:** Walk Trastevere and Via Margutta, where Roman Holiday was filmed.\n\n"
    "**Day 3:** See the Trevi Fountain from La Dolce Vita, then the Vatican Museums."
)


# --------------------------------------------------------------------------- F02 classic: 22 s waiting for Groq, then a digest


# --------------------------------------------------------------------------- F05 classic: the dubbing question looked up
