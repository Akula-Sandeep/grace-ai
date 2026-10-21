"""Defects found by the third live evaluation (owner's machine, 2026-10-13 21:36 IST).

Report: eval-reports/20261013-213602/{model,classic}. Each test reproduces one
defect offline (fixed texts, a fake catalogue or a fake provider); none of this
is a live check.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import working_state as working
from app.agent.router import QueryClassification
from app.services.recommendation_engine import recommend

# --------------------------------------------------------------------------- R01, classic mode: "add it" added the wrong film

WHIPLASH = {"tmdb_id": 244786, "title": "Whiplash", "runtime": 107}
TERMINATOR = {"tmdb_id": 218, "title": "The Terminator", "runtime": 108}
IMITATION = {"tmdb_id": 205596, "title": "The Imitation Game", "runtime": 114}
PRISONERS = {"tmdb_id": 146233, "title": "Prisoners", "runtime": 153}

# The replies as the live run showed them (trimmed).
FIRST_REPLY = """Here are three thriller titles that fit your < 2-hour limit:

| Film | Year | Runtime | Why it fits |
|------|------|---------|-------------|
| **Whiplash** (2014) | 107 min | A tense drama-music thriller |
| **The Terminator** (1984) | 108 min | Classic sci-fi thriller |
| **The Imitation Game** (2014) | 113 min | Historical thriller |
"""
SECOND_REPLY = """Below are three **near-matches** that satisfy the runtime and genre constraints:

| Film | Year | Runtime | How it relaxes the "dark" constraint |
|------|------|---------|--------------------------------------|
| **The Terminator** (1984) | 108 min | More gritty-action than deeply dark. |
| **The Imitation Game** (2014) | 113 min | Serious, but leans inspirational. |
| **Parasite** (2019) | 132 min* | Exceeds the two-hour limit by about 12 minutes. |
"""
THIRD_REPLY = "**The Imitation Game** (2014) is a historical thriller that follows Alan Turing."


def _turn(state, references, reply, *, acted=False):
    working.start_turn(state)
    working.record_references(state, references)
    working.order_by_answer(state, reply, acted=acted)


def _add():
    return SimpleNamespace(intent="add_to_watchlist", movie_titles=[], movie_title=None, year=None)


def _r01_state():
    state = working.empty_state()
    # The recommender returns more candidates than the reply shows ...
    _turn(state, [WHIPLASH, TERMINATOR, IMITATION, PRISONERS], FIRST_REPLY)
    # ... and "Not the first one. Something darker." re-ranked a subset of them,
    # while the reply added a film of the model's own (Parasite).
    _turn(state, [TERMINATOR, IMITATION, PRISONERS], SECOND_REPLY)
    return state


def test_acting_on_a_film_from_the_list_keeps_the_list():
    state = working.empty_state()
    _turn(state, [WHIPLASH, TERMINATOR, IMITATION], FIRST_REPLY)
    _turn(state, [TERMINATOR], "Added **The Terminator** (1984) to your watchlist.", acted=True)
    working.start_turn(state)
    assert [m["title"] for m in state["last_answer_movies"]] == [
        "Whiplash",
        "The Terminator",
        "The Imitation Game",
    ]
    assert state["focus"]["title"] == "The Terminator"
    assert state["handled"] == ["The Terminator"]


def test_the_listed_flag_is_saved_with_the_reply():
    state = _r01_state()
    assert working.snapshot(state)["turn_listed"] is True
    restored = working.normalize(working.snapshot(state))
    working.start_turn(restored)
    assert [m["title"] for m in restored["last_answer_movies"]][1] == "The Imitation Game"


# --------------------------------------------------------------------------- F04 / M01: the older recommendation path found nothing


def _film(mid, title, *, genres, year, votes, lang="en", popularity=10.0, runtime=120, overview=""):
    return {
        "id": mid,
        "title": title,
        "runtime": runtime,
        "genre_ids": list(genres),
        "genres": [{"id": g} for g in genres],
        "release_date": f"{year}-06-01",
        "vote_count": votes,
        "popularity": popularity,
        "vote_average": 7.6,
        "original_language": lang,
        "overview": overview,
        "keywords": {"keywords": []},
    }


class _DiscoverTMDB:
    """Applies the /discover filters the way TMDB does, including the vote floor."""

    def __init__(self, catalog):
        self.catalog = {m["id"]: m for m in catalog}
        self.discover_calls: list[dict] = []

    async def discover_movies(self, **kw):
        self.discover_calls.append(kw)
        if kw.get("page", 1) > 1:
            return {"results": []}
        movies = list(self.catalog.values())
        if kw.get("vote_count_min") is not None:
            movies = [m for m in movies if m["vote_count"] >= kw["vote_count_min"]]
        if kw.get("original_language"):
            movies = [m for m in movies if m["original_language"] == kw["original_language"]]
        if kw.get("genre_ids"):
            movies = [m for m in movies if set(kw["genre_ids"]) <= set(m["genre_ids"])]
        if kw.get("any_genre_ids"):
            movies = [m for m in movies if set(kw["any_genre_ids"]) & set(m["genre_ids"])]
        if kw.get("release_year_min"):
            movies = [m for m in movies if int(m["release_date"][:4]) >= kw["release_year_min"]]
        if kw.get("release_year_max"):
            movies = [m for m in movies if int(m["release_date"][:4]) <= kw["release_year_max"]]
        movies.sort(key=lambda m: m["popularity"], reverse=True)
        return {"results": [{k: v for k, v in m.items() if k != "runtime"} for m in movies[:20]]}

    async def get_movie_details(self, movie_id):
        return dict(self.catalog[movie_id])

    async def get_recommendations(self, movie_id, page=1):
        return {"results": []}

    async def get_similar_movies(self, movie_id, page=1):
        return {"results": []}


def _ask(**fields):
    return QueryClassification(domain="movie", intent="recommendation", needs_live_search=False, **fields)


TAMIL = [
    # TMDB vote counts for Tamil films are in the tens to low hundreds.
    _film(1, "Ratsasan", genres=(53, 80), year=2018, votes=240, lang="ta", popularity=9),
    _film(2, "Vikram Vedha", genres=(53, 28, 80), year=2017, votes=210, lang="ta", popularity=8),
    _film(3, "Kaithi", genres=(28, 53), year=2019, votes=190, lang="ta", popularity=7),
    _film(4, "Thuppakki", genres=(28, 53), year=2012, votes=60, lang="ta", popularity=4),
    _film(5, "Old Tamil Thriller", genres=(53,), year=1998, votes=80, lang="ta", popularity=3),
    _film(6, "Hindi Thriller", genres=(53,), year=2015, votes=900, lang="hi", popularity=20),
    _film(7, "Gone Girl", genres=(53, 9648), year=2014, votes=19000, popularity=60),
]


@pytest.mark.asyncio
async def test_tamil_thrillers_of_the_2010s_are_found_despite_few_tmdb_votes():
    tmdb = _DiscoverTMDB(TAMIL)
    ask = _ask(genres=["Thriller"], original_language="ta", min_year=2010, max_year=2019)
    result = await recommend(ask, tmdb=tmdb, limit=2)
    titles = [m["title"] for m in result["strict_matches"]]
    assert result["status"] == "found"
    assert titles == ["Ratsasan", "Vikram Vedha"]  # live: none (300-vote floor)
    assert all(m["original_language"] == "ta" for m in result["strict_matches"])
    assert tmdb.discover_calls[0]["vote_count_min"] == 25
    assert tmdb.discover_calls[0]["original_language"] == "ta"


@pytest.mark.asyncio
async def test_a_thin_first_pass_is_widened_once_with_a_lower_vote_floor():
    westerns = [
        _film(10 + i, f"Western {i}", genres=(37,), year=1965, votes=v, popularity=5 - i * 0.1)
        for i, v in enumerate((520, 150, 120, 90, 70))
    ]
    tmdb = _DiscoverTMDB(westerns)
    result = await recommend(_ask(genres=["Western"], min_year=1960, max_year=1969), tmdb=tmdb, limit=3)
    assert len(result["strict_matches"]) == 3
    assert [c["vote_count_min"] for c in tmdb.discover_calls] == [300, 300, 40]
    assert result["widened_vote_floor"] == 40


@pytest.mark.asyncio
async def test_a_full_first_pass_is_not_widened():
    tmdb = _DiscoverTMDB(TAMIL)
    await recommend(_ask(genres=["Thriller"]), tmdb=tmdb, limit=1)
    assert all(c["vote_count_min"] == 300 for c in tmdb.discover_calls)


FEEL_GOOD_POOL = [
    _film(20, "Grim Blockbuster", genres=(28, 53), year=2025, votes=5000, popularity=900),
    _film(21, "Horror Hit", genres=(27,), year=2025, votes=4000, popularity=800),
    _film(22, "War Epic", genres=(10752, 18), year=2024, votes=6000, popularity=700),
    _film(
        23,
        "Paddington 2",
        genres=(10751, 35, 12),
        year=2017,
        votes=4000,
        popularity=40,
        overview="Paddington's good deeds turn everyday mishaps into joy.",
    ),
    _film(24, "The Intouchables", genres=(18, 35), year=2011, votes=17000, popularity=50),
    _film(25, "Amélie", genres=(35, 10749), year=2001, votes=11000, popularity=45),
]


@pytest.mark.asyncio
async def test_feel_good_picks_come_from_the_genres_the_mood_leans_to():
    tmdb = _DiscoverTMDB(FEEL_GOOD_POOL)
    result = await recommend(_ask(mood_keywords=["feel-good"]), tmdb=tmdb, limit=3)
    titles = [m["title"] for m in result["strict_matches"]]
    assert set(titles) == {"Paddington 2", "The Intouchables", "Amélie"}
    mood_call = next(c for c in tmdb.discover_calls if c.get("any_genre_ids"))
    assert {35, 10751} <= set(mood_call["any_genre_ids"])


@pytest.mark.asyncio
async def test_a_mood_never_filters_out_a_requested_genre():
    tmdb = _DiscoverTMDB(FEEL_GOOD_POOL)
    result = await recommend(_ask(genres=["Action"], mood_keywords=["feel-good"]), tmdb=tmdb, limit=3)
    assert [m["title"] for m in result["strict_matches"]] == ["Grim Blockbuster"]
    assert not any(c.get("any_genre_ids") for c in tmdb.discover_calls)


@pytest.mark.asyncio
async def test_the_tmdb_client_sends_a_moods_genres_as_any_of(monkeypatch):
    from app.services.tmdb_client import TMDBClient

    client = TMDBClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return {"results": []}

    monkeypatch.setattr(client, "_get", fake_get)
    await client.discover_movies(any_genre_ids=[35, 10751])
    assert calls[0][1]["with_genres"] == "35|10751"
    await client.discover_movies(genre_ids=[53], any_genre_ids=[35])
    assert calls[1][1]["with_genres"] == "53"


def test_the_recommendation_rules_say_moods_only_rank():
    import inspect

    from app.agent import model_tools

    source = inspect.getsource(model_tools)
    assert "Moods only ordered" in source and "say nothing fits only when there are no matches" in source


# --------------------------------------------------------------------------- Gemini: gemini-3.8-flash answered 503 in all three runs


def _api_error(code, status, details=None):
    from google.genai import errors

    body = {"error": {"code": code, "status": status, "message": "x", "details": details or []}}
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body)


def _grounded(text="Denis Villeneuve directed Arrival."):
    chunk = SimpleNamespace(web=SimpleNamespace(uri="https://example.org/arrival", title="example.org"))
    metadata = SimpleNamespace(grounding_chunks=[chunk], grounding_supports=[])
    return SimpleNamespace(
        text=text, candidates=[SimpleNamespace(grounding_metadata=metadata)], usage_metadata=None
    )


@pytest.fixture
def gemini(monkeypatch):
    """A fake google-genai client; ``failures`` maps a model to the error it raises."""
    from app.services import movie_data_fallback as fallback
    from app.services import resilience

    resilience.reset()
    fallback._GEMINI_MODEL_REST.clear()
    fallback._GEMINI_LAST_GOOD.clear()
    monkeypatch.setattr(fallback.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(fallback.settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(fallback.settings, "gemini_fallback_models", "gemini-3.7-flash,gemini-2.5-flash")
    world = SimpleNamespace(failures={}, tried=[], answer=_grounded)

    class Models:
        async def generate_content(self, *, model, contents, config=None):
            world.tried.append(model)
            if model in world.failures:
                raise world.failures[model]
            return world.answer()

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


@pytest.mark.asyncio
async def test_grounded_search_moves_to_the_next_model_when_the_first_is_overloaded(gemini):
    from app.services.movie_data_fallback import MovieDataFallbackEngine, gemini_model_used

    gemini.failures["gemini-3.8-flash"] = _api_error(503, "UNAVAILABLE")
    result = await MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert result and result["sources"] == ["https://example.org/arrival"]
    assert gemini.tried == ["gemini-3.8-flash", "gemini-3.7-flash"]
    assert gemini_model_used() == "gemini-3.7-flash"
    # The overloaded model rests: the next call starts with the one that answered.
    gemini.tried.clear()
    await MovieDataFallbackEngine.grounded_search("Who wrote Arrival?")
    assert gemini.tried == ["gemini-3.7-flash"]


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_retried_on_other_models(gemini):
    from app.services.movie_data_fallback import MovieDataFallbackEngine
    from app.services.upstream_errors import UpstreamUnavailable

    gemini.failures["gemini-3.8-flash"] = _api_error(403, "PERMISSION_DENIED")
    with pytest.raises(UpstreamUnavailable):
        await MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert gemini.tried == ["gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_when_every_model_fails_the_last_error_is_raised(gemini):
    from app.services.movie_data_fallback import MovieDataFallbackEngine
    from app.services.upstream_errors import UpstreamUnavailable

    for model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"):
        gemini.failures[model] = _api_error(503, "UNAVAILABLE")
    with pytest.raises(UpstreamUnavailable, match="server_error"):
        await MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert gemini.tried == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_no_fallback_models_means_one_try(gemini, monkeypatch):
    from app.services import movie_data_fallback as fallback
    from app.services.upstream_errors import UpstreamUnavailable

    monkeypatch.setattr(fallback.settings, "gemini_fallback_models", "")
    gemini.failures["gemini-3.8-flash"] = _api_error(503, "UNAVAILABLE")
    with pytest.raises(UpstreamUnavailable):
        await fallback.MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert gemini.tried == ["gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_the_live_check_probes_the_configured_fallbacks(gemini, monkeypatch):
    from app.services import diagnostics

    gemini.failures["gemini-3.8-flash"] = _api_error(503, "UNAVAILABLE")

    async def no_listing(_client, tries=2):
        return []

    monkeypatch.setattr(diagnostics, "_gemini_alternatives", no_listing)
    results = await diagnostics._check_gemini()
    by_service = {r["service"]: r for r in results}
    assert by_service["Gemini (gemini-3.8-flash)"]["ok"] is False
    assert by_service["Gemini fallback gemini-3.7-flash"]["ok"] is True
    assert by_service["Gemini fallback gemini-2.5-flash"]["ok"] is True
    grounded = by_service["Gemini grounded web search"]
    assert grounded["ok"] is True and "answered by gemini-3.7-flash" in grounded["detail"]


@pytest.mark.asyncio
async def test_when_every_model_is_resting_only_the_first_to_wake_is_tried(gemini):
    from app.services import movie_data_fallback as fallback
    from app.services.upstream_errors import UpstreamUnavailable

    for model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"):
        gemini.failures[model] = _api_error(503, "UNAVAILABLE")
    with pytest.raises(UpstreamUnavailable):
        await fallback.MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    gemini.tried.clear()
    with pytest.raises(UpstreamUnavailable):
        await fallback.MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert gemini.tried == ["gemini-3.8-flash"]


# --------------------------------------------------------------------------- model mode: F06 language, R02 rating label, R03 region, S01


def test_the_tool_policy_names_rating_source_region_and_reply_language():
    from app.core.prompts import GRACE_SYSTEM_PROMPT

    # In the shared system prompt since round 7j, so the older path's answers follow them too.
    assert "TMDB user scores" in GRACE_SYSTEM_PROMPT and "never IMDb" in GRACE_SYSTEM_PROMPT
    assert "for the region it names" in GRACE_SYSTEM_PROMPT
    assert "Reply in the language and script of the user's latest message" in GRACE_SYSTEM_PROMPT
    assert "When you decline a request, say in one sentence what you can help with" in GRACE_SYSTEM_PROMPT


# --------------------------------------------------------------------------- R02, classic mode: follow-ups after a comparison

DUNE = {"title": "Dune", "year": "2021", "tmdb_id": 438631, "runtime": 155}
INTERSTELLAR = {"title": "Interstellar", "year": "2014", "tmdb_id": 157336, "runtime": 169}


def _compared():
    state = working.empty_state()
    working.record_comparison(state, [DUNE, INTERSTELLAR], ["critical reception"])
    return state


def _classified(intent, **fields):
    domain = "general" if intent in {"casual_chat", "general_fact"} else "movie"
    return QueryClassification(domain=domain, intent=intent, needs_live_search=False, **fields)


def test_a_comparison_is_remembered_with_years_and_runtimes():
    state = _compared()
    assert state["comparison"]["titles"] == ["Dune (2021)", "Interstellar (2014)"]
    assert [f["runtime"] for f in state["comparison"]["films"]] == [155, 169]


# --------------------------------------------------------------------------- C01, classic mode: the first message was out of reach


# --------------------------------------------------------------------------- F01/F02/F05, classic mode: a failed web search ended the turn in an error


class _NoWebEngine:
    """The movie database works; the web search does not (Gemini 503)."""

    def __init__(self):
        self.lookups = []

    async def grounded_search(self, query):
        from app.services.upstream_errors import UpstreamUnavailable

        raise UpstreamUnavailable("Gemini grounded search failed (server_error): 503", provider="gemini")

    async def lookup_query(self, classification, user_message, **kwargs):
        self.lookups.append((classification, kwargs))
        return {
            "status": "found",
            "details": {"id": 496243, "title": "Parasite", "release_date": "2019-05-30"},
            "watch_providers": {"region": "NL", "flatrate": [{"provider_name": "Example+"}]},
        }

    async def close(self):
        return None


async def _no_web_turn(monkeypatch, message, *, focus=None, movie_title=None, domain="general"):
    from app.agent import dispatcher
    from app.services.movie_data_fallback import MovieDataFallbackEngine
    from tests.model_harness import ScriptedModel

    engine = _NoWebEngine()
    monkeypatch.setattr(
        MovieDataFallbackEngine, "grounded_search", classmethod(lambda cls, q: engine.grounded_search(q))
    )
    model = ScriptedModel()
    router = SimpleNamespace(fast=lambda: model, deep=lambda: model, vision=lambda: model)
    state = working.empty_state()
    if focus:
        state["focus"] = {"title": focus}
    classification = QueryClassification(
        domain=domain,
        intent="general_fact" if domain == "general" else "metadata",
        needs_live_search=True,
        search_query=message,
        movie_title=movie_title,
    )
    chunks = [
        chunk
        async for chunk in dispatcher._dispatch_and_stream_impl(
            message,
            [],
            classification=classification,
            router=router,
            movie_engine=engine,
            working_state=state,
        )
    ]
    return "".join(c for c in chunks if not c.startswith("event: ")), engine, model


@pytest.mark.parametrize(
    "reference, searched, expected",
    [
        ("Dune (2021)", "Dune", 438631),  # the year in brackets picks the film, not the search
        ("Dune (1984)", "Dune", 841),
        ("Blade Runner 2049", "Blade Runner 2049", 335984),  # a year that is part of the title
    ],
)
@pytest.mark.asyncio
async def test_a_reference_with_its_year_is_searched_by_title(reference, searched, expected):
    from app.agent.models import ExecutionPlan, ExecutionPolicy, ExecutionState, ExecutionTask
    from app.agent.task_handlers import MovieTaskHandlers

    catalog = [
        {"id": 438631, "title": "Dune", "release_date": "2021-09-15"},
        {"id": 841, "title": "Dune", "release_date": "1984-12-14"},
        {"id": 78, "title": "Blade Runner", "release_date": "1982-06-25"},
        {"id": 335984, "title": "Blade Runner 2049", "release_date": "2017-10-04"},
    ]
    queries = []

    class Tools:
        async def search_movies(self, query):
            queries.append(query)
            words = query.casefold()
            return {"results": [m for m in catalog if words in m["title"].casefold()]}

    task = ExecutionTask(id="r", type="resolve_movie_references", input={"references": [reference]})
    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(path="fast", reasoning_level="none", evidence_level="structured")
        )
    )
    result = await MovieTaskHandlers(tools=Tools()).resolve_movie_references(task, state)
    assert queries == [searched]
    assert (result["references"][0]["resolved"] or {}).get("id") == expected


@pytest.mark.asyncio
async def test_a_models_own_quota_moves_on_and_rests_it_for_the_day(gemini):
    from app.services import movie_data_fallback as fallback

    quota = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
        }
    ]
    gemini.failures["gemini-3.8-flash"] = _api_error(429, "RESOURCE_EXHAUSTED", quota)
    gemini.failures["gemini-3.7-flash"] = _api_error(404, "NOT_FOUND")
    result = await fallback.MovieDataFallbackEngine.grounded_search("Who directed Arrival?")
    assert result
    assert gemini.tried == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"]
    import time

    # Keyed by kind of call (round 7k): web search is a "grounded" call.
    assert fallback._GEMINI_MODEL_REST[("grounded", "gemini-3.8-flash")] - time.monotonic() > 3000
    assert fallback._GEMINI_MODEL_REST[("grounded", "gemini-3.7-flash")] - time.monotonic() > 3000
