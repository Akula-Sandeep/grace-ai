"""Defects found by the fifth live evaluation (owner's machine, 2026-10-14 13:22 IST).

Report: eval-reports/20261014-132223/{model,classic}. Each test reproduces one
defect offline (a scripted model, a fake catalogue or a fake provider); none of
this is a live check.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.jarvis_harness import film, install
from tests.model_harness import ScriptedModel

DIGEST = (
    "_Grace’s AI is busy for a moment, so this comes straight from the movie database._\n\n"
    "**Parasite** (2019) · 133 min · Comedy, Thriller, Drama\n\nDirected by Bong Joon Ho."
)


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


# --------------------------------------------------------------------------- F02, classic: "Where can I stream it?" with no title


# --------------------------------------------------------------------------- Gemini: lite answers plain requests, grounded search is out of quota


def _api_error(code, status):
    from google.genai import errors

    body = {"error": {"code": code, "status": status, "message": "x", "details": []}}
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body)


@pytest.fixture
def gemini(monkeypatch):
    """A fake google-genai client. ``plain``/``grounded`` map a model to its error, per kind of call."""
    from app.services import movie_data_fallback as fallback
    from app.services import resilience

    resilience.reset()
    fallback._GEMINI_MODEL_REST.clear()
    fallback._GEMINI_LAST_GOOD.clear()
    monkeypatch.setattr(fallback.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(fallback.settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(
        fallback.settings,
        "gemini_fallback_models",
        "gemini-3.7-flash,gemini-flash-lite-latest,gemini-3.5-flash",
    )
    world = SimpleNamespace(plain={}, grounded={}, tried=[], configs=[])

    class Models:
        async def generate_content(self, *, model, contents, config=None):
            grounded = bool(getattr(config, "tools", None))
            world.tried.append((("grounded" if grounded else "plain"), model))
            world.configs.append((contents, config))
            failure = (world.grounded if grounded else world.plain).get(model)
            if failure is not None:
                raise failure
            return SimpleNamespace(text=f"answer from {model}", candidates=[], usage_metadata=None)

    class Client:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=Models(), aclose=self._close)

        async def _close(self):
            return None

    monkeypatch.setattr("google.genai.Client", Client)
    yield world
    resilience.reset()
    fallback._GEMINI_MODEL_REST.clear()
    fallback._GEMINI_LAST_GOOD.clear()


def _overloaded(world, *models, kind="plain"):
    for model in models:
        getattr(world, kind)[model] = _api_error(503, "UNAVAILABLE")


@pytest.mark.asyncio
async def test_the_live_check_primes_the_working_model(gemini, monkeypatch):
    from app.services import diagnostics
    from app.services import movie_data_fallback as fallback

    _overloaded(gemini, "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash")

    async def no_listing(_client, tries=2):
        return []

    monkeypatch.setattr(diagnostics, "_gemini_alternatives", no_listing)
    await diagnostics._check_gemini()
    assert fallback.gemini_models("plain")[0] == "gemini-flash-lite-latest"


# --------------------------------------------------------------------------- every Groq model at its limit: Gemini writes the answer


class _Busy(RuntimeError):
    status_code = 429


class _BusyProvider:
    model_name = "openai/gpt-oss-120b"
    provider_name = "groq"

    async def stream(self, _request):
        raise _Busy("rate limited")
        yield ""  # pragma: no cover


_BUSY_ROUTER = SimpleNamespace(fast=lambda: _BusyProvider(), deep=lambda: _BusyProvider())

PARASITE_EVIDENCE = {
    "status": "found",
    "movie": {
        "tmdb_id": 496243,
        "title": "Parasite",
        "release_date": "2019-05-30",
        "runtime": 133,
        "overview": "All unemployed, Ki-taek's family takes peculiar interest in the wealthy Parks.",
        "directors": ["Bong Joon Ho"],
    },
}


# --------------------------------------------------------------------------- model mode: a limit after the tools ran


@pytest.fixture
def scripted(monkeypatch):
    from tests.jarvis_harness import Conversation, new_user
    from tests.model_harness import install_model

    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    return Conversation(factory, new_user(factory)), model


# --------------------------------------------------------------------------- G04: invented film locations


# --------------------------------------------------------------------------- M01: Coraline as "feel-good"


@pytest.mark.asyncio
async def test_a_horror_tinged_film_ranks_below_plain_feel_good_picks():
    from app.services.recommendation_engine import recommend
    from tests.test_live_run3_regressions import _ask, _DiscoverTMDB, _film

    pool = [
        _film(30, "Coraline", genres=(16, 10751, 14, 27), year=2009, votes=9000, popularity=60),
        _film(31, "Paddington 2", genres=(10751, 35, 12), year=2017, votes=4000, popularity=40),
        _film(32, "Coco", genres=(16, 10751, 10402), year=2017, votes=20000, popularity=55),
        _film(33, "The Intouchables", genres=(18, 35), year=2011, votes=17000, popularity=50),
    ]
    result = await recommend(_ask(mood_keywords=["feel-good"]), tmdb=_DiscoverTMDB(pool), limit=3)
    titles = [m["title"] for m in result["strict_matches"]]
    assert "Coraline" not in titles  # live runs 4-5: offered as feel-good


def test_the_prompt_asks_to_say_which_film_an_ambiguous_title_means():
    from app.core.prompts import GRACE_SYSTEM_PROMPT

    assert (
        "When a title could mean several films, say in a few words which one you mean" in GRACE_SYSTEM_PROMPT
    )


# --------------------------------------------------------------------------- P02: "which of their other films is the shortest?"


@pytest.mark.asyncio
async def test_a_person_lookup_adds_runtimes_only_for_a_question_about_length(monkeypatch):
    from app.agent.router import QueryClassification
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    _factory, tmdb = install(monkeypatch, offline=True)
    engine = MovieDataFallbackEngine(tmdb=tmdb)
    ask = QueryClassification(
        domain="movie", intent="cast_crew", needs_live_search=False, person_name="Denis Villeneuve"
    )
    timed = await engine.lookup_query(ask, "Which of Villeneuve's films is the shortest?")
    plain = await engine.lookup_query(ask, "Who is Denis Villeneuve?")
    runtimes = [film["runtime"] for film in timed["films_with_runtimes"]]
    assert runtimes == sorted(runtimes) and len(runtimes) >= 3
    assert "films_with_runtimes" not in plain  # no extra lookups otherwise


@pytest.mark.parametrize(
    "answer, canned",
    [
        (
            "_Grace’s AI is busy for a moment, so this comes straight from the movie database._\n\n**Dune**",
            True,
        ),
        ("_Grace’s AI isn’t available right now, so this comes straight from the movie database._", True),
        ("I can't reach my AI service right now, so open-ended chat is limited.", True),
        (
            "_Grace’s main AI is busy for a moment, so the backup model answered; it wasn’t checked._\n\nCanberra.",
            False,
        ),
        (
            "_Grace’s main AI isn’t available right now, so this answer is from the backup model._\n\nCanberra.",
            False,
        ),
        ("The capital of Australia is **Canberra**.", False),
    ],
)
def test_the_report_counts_database_digests_not_backup_answers_as_canned(answer, canned):
    from app.evals.live_agent import _CANNED

    assert bool(_CANNED.search(answer)) is canned


@pytest.mark.asyncio
async def test_the_model_that_answered_last_is_tried_first(gemini):
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    _overloaded(gemini, "gemini-3.8-flash", "gemini-3.7-flash", kind="grounded")
    await MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    gemini.tried.clear()
    gemini.grounded.clear()  # the overloaded models recovered, but the working one goes first
    await MovieDataFallbackEngine.grounded_search("Who directed Sicario?")
    assert gemini.tried == [("grounded", "gemini-flash-lite-latest")]


@pytest.mark.asyncio
async def test_a_model_that_times_out_is_rested_and_the_next_one_answers(gemini):
    import httpx

    from app.services.movie_data_fallback import MovieDataFallbackEngine

    gemini.grounded["gemini-3.8-flash"] = httpx.ReadTimeout("timed out")
    _overloaded(gemini, "gemini-3.7-flash", kind="grounded")
    await MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert gemini.tried[-1] == ("grounded", "gemini-flash-lite-latest")
    gemini.tried.clear()
    await MovieDataFallbackEngine.grounded_search("Who directed Sicario?")
    assert ("grounded", "gemini-3.8-flash") not in gemini.tried  # resting after its timeout
