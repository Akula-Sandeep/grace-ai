"""The model's tools (agent/model_tools.py) on their own, with the movie engine faked.

Each tool must return structured data or a clear status the model can act on, and
never raise: a failure is a result, so the model can say what it could not verify.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent import working_state as working
from app.agent.model_tools import TurnTools, _brief, _providers_summary, movie_card, status_event
from app.inference.models import ToolCall
from app.services.upstream_errors import UpstreamUnavailable

ARRIVAL = {
    "id": 329865,
    "title": "Arrival",
    "release_date": "2016-11-10",
    "runtime": 116,
    "genres": [{"name": "Science Fiction"}],
    "credits": {"crew": [{"job": "Director", "name": "Denis Villeneuve"}], "cast": []},
    "release_dates": {
        "results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
            {"iso_3166_1": "NL", "release_dates": [{"certification": "12"}]},
        ]
    },
    "belongs_to_collection": None,
    "translations": {
        "translations": [{"iso_639_1": "hi", "data": {"title": "अराइवल", "overview": "भाषा की कहानी"}}]
    },
}


class FakeTMDB:
    def __init__(self):
        self.details: dict | BaseException = ARRIVAL
        self.providers: dict | BaseException = {"flatrate": [{"provider_name": "Netflix"}]}

    async def get_movie_details(self, movie_id):
        if isinstance(self.details, BaseException):
            raise self.details
        return self.details

    async def get_watch_providers(self, movie_id, region=None):
        if isinstance(self.providers, BaseException):
            raise self.providers
        return self.providers


class FakeEngine:
    def __init__(self):
        self.tmdb_client = FakeTMDB()
        self.lookup: dict = {"status": "found", "details": ARRIVAL}
        self.query_result: object = None
        self.closed = False

    async def resolve_movie(self, title, year=None):
        return self.lookup

    async def lookup_query(self, classification, message, **kwargs):
        self.last_query = (classification, kwargs)
        return self.query_result

    async def close(self):
        self.closed = True


def tools(**kwargs) -> TurnTools:
    return TurnTools(
        user_message=kwargs.pop("message", "tell me about arrival"),
        chat_history=kwargs.pop("history", []),
        state=kwargs.pop("state", working.empty_state()),
        router=None,
        movie_engine=kwargs.pop("engine", FakeEngine()),
        **kwargs,
    )


def test_status_lines_name_the_film_and_fall_back_when_the_label_cannot_be_built():
    assert "Looking up Arrival" in status_event(
        ToolCall(id="1", name="movie_details", arguments={"title": "Arrival"})
    )
    assert "Working…" in status_event(ToolCall(id="2", name="nonexistent", arguments={}))


def test_a_recommended_film_is_trimmed_for_the_model():
    brief = _brief({"title": "X", "overview": "word " * 100, "keywords": list("abcdefg")})
    assert len(brief["overview"]) <= 241 and brief["overview"].endswith("…")
    assert brief["keywords"] == list("abcde")
    assert _brief("not a dict") == "not a dict"


def test_movie_cards_carry_the_regional_certification_and_providers_are_summarised():
    card = movie_card(ARRIVAL, region="NL")
    assert card["certification"] == "12 (NL)"
    assert movie_card(ARRIVAL, region="FR")["certification"] == "PG-13 (US)"
    assert _providers_summary(None) is None
    assert _providers_summary({"flatrate": [{"provider_name": "Netflix"}], "rent": []}) == {
        "stream": ["Netflix"]
    }


@pytest.mark.asyncio
async def test_movie_details_by_title_with_translation_and_where_to_watch():
    t = tools()
    result = await t.movie_details({"title": "Arrival", "language": "hi", "where_to_watch": True})
    movie = result["movie"]
    assert result["status"] == "found" and movie["localized_title"] == "अराइवल"
    assert movie["where_to_watch"] == {"stream": ["Netflix"]}


@pytest.mark.asyncio
async def test_where_to_watch_that_cannot_be_checked_says_so():
    engine = FakeEngine()
    engine.tmdb_client.providers = httpx.ReadTimeout("slow")
    result = await tools(engine=engine).movie_details({"title": "Arrival", "where_to_watch": True})
    assert result["movie"]["where_to_watch"] == {"error": "Availability could not be checked right now."}


@pytest.mark.asyncio
async def test_movie_details_by_id_falls_back_to_the_title_and_reports_what_it_could_not_find():
    engine = FakeEngine()
    engine.tmdb_client.details = httpx.ReadTimeout("slow")
    t = tools(engine=engine)
    assert (await t.movie_details({"tmdb_id": 329865, "title": "Arrival"}))["status"] == "found"
    assert await t.movie_details({"tmdb_id": 329865}) == {"status": "needs_title"}
    engine.tmdb_client.details = UpstreamUnavailable("down", provider="tmdb")
    with pytest.raises(UpstreamUnavailable):
        await t.movie_details({"tmdb_id": 329865})
    engine.lookup = {
        "status": "ambiguous",
        "candidates": [
            {"tmdb_id": 438631, "title": "Dune", "release_date": "2021-09-15"},
            {"tmdb_id": 841, "title": "Dune", "original_title": "Dune", "release_date": "1984-12-14"},
            "noise",
        ],
    }
    ambiguous = await t.movie_details({"title": "Dune"})
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidates"] == [
        {"tmdb_id": 438631, "title": "Dune", "year": 2021},
        {"tmdb_id": 841, "title": "Dune", "year": 1984},
    ]
    engine.lookup = {"status": "not_found"}
    assert await t.movie_details({"title": "Nothing"}) == {"status": "not_found", "requested": "Nothing"}


@pytest.mark.asyncio
async def test_person_details_needs_a_name_and_reports_a_miss():
    engine = FakeEngine()
    t = tools(engine=engine)
    assert await t.person_details({}) == {"status": "needs_name"}
    assert await t.person_details({"name": "Nobody"}) == {"status": "not_found", "requested": "Nobody"}
    seen = []
    engine.query_result = {"person": "Denis Villeneuve", "person_id": 137427, "films": []}
    t.references = seen.extend
    result = await t.person_details({"name": "Denis Villeneuve"})
    assert result["person"] == "Denis Villeneuve"
    assert seen == [{"kind": "person", "person_id": 137427, "title": "Denis Villeneuve"}]


@pytest.mark.asyncio
async def test_research_needs_a_title_and_names_an_unsupported_topic():
    t = tools()
    assert await t.movie_research({"topic": "awards"}) == {"status": "needs_title"}
    assert await t.movie_research({"topic": "gossip", "title": "Arrival"}) == {
        "status": "unsupported_topic",
        "topic": "gossip",
    }


@pytest.mark.asyncio
async def test_reviews_for_a_film_that_cannot_be_found_say_so():
    engine = FakeEngine()
    engine.lookup = {"status": "not_found"}
    assert await tools(engine=engine).movie_research({"topic": "reviews", "title": "Nothing"}) == {
        "status": "not_found",
        "requested": "Nothing",
    }


@pytest.mark.asyncio
async def test_compare_needs_two_titles_and_web_search_needs_a_query_and_a_key(monkeypatch):
    from app.config import settings

    t = tools()
    assert await t.compare_movies({"titles": ["Dune"]}) == {"status": "needs_two_titles"}
    assert await t.web_search({"query": " "}) == {"status": "needs_query"}
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    assert (await t.web_search({"query": "box office of Dune"}))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_guests_have_a_session_watchlist_only():
    t = tools()
    assert (await t.my_lists({"list": "watched"}))["status"] == "needs_account"
    assert (await t.recommend_movies({"from_watchlist": True}))["status"] == "needs_account"


@pytest.mark.asyncio
async def test_a_guest_recalls_from_this_conversation():
    history = [
        {"role": "user", "content": "I loved Arrival's ending"},
        {"role": "assistant", "content": "It's about grief and time."},
        {"role": "user", "content": "unrelated"},
    ]
    t = tools(history=history)
    assert await t.recall_conversation({}) == {"status": "needs_query"}
    found = await t.recall_conversation({"query": "Arrival ending"})
    assert [m["content"] for m in found["messages"]] == ["I loved Arrival's ending"]


@pytest.mark.asyncio
async def test_a_list_change_needs_a_known_action_and_a_film():
    t = tools()
    out = [item async for item in t.update_my_list({"action": "teleport"})]
    assert out == [("result", {"status": "error", "error": "Unknown action 'teleport'."})]
    out = [item async for item in t.update_my_list({"action": "add_to_watchlist", "movies": [{}]})]
    assert out[-1][1]["status"] == "needs_movie"


@pytest.mark.asyncio
async def test_only_one_change_can_wait_for_a_yes_and_a_proposal_uses_the_known_title():
    state = working.empty_state()
    state["movies"] = [{"tmdb_id": 329865, "title": "Arrival", "year": 2016}]
    t = tools(state=state)
    first = [
        item
        async for item in t.update_my_list(
            {"action": "rate", "rating": 42, "movies": [{"tmdb_id": 329865}], "ask_first": True}
        )
    ]
    proposed = first[-1][1]
    assert proposed["status"] == "proposed" and proposed["proposed"]["movie"]["title"] == "Arrival"
    assert "rating" not in t.state["pending"]  # 42 is not a 0-10 rating
    second = [
        item
        async for item in t.update_my_list(
            {"action": "mark_watched", "movies": [{"title": "Dune"}], "ask_first": True}
        )
    ]
    assert second[-1][1]["status"] == "not_done"


@pytest.mark.asyncio
async def test_the_runner_turns_every_failure_into_a_result_and_keeps_the_budget(monkeypatch):
    from app.config import settings

    t = tools()

    async def upstream(args):
        raise UpstreamUnavailable("down", provider="tmdb")

    async def broken(args):
        raise ValueError("bad data")

    monkeypatch.setattr(t, "movie_details", upstream)
    monkeypatch.setattr(t, "person_details", broken)
    monkeypatch.setattr(settings, "max_tool_calls", 4)
    calls = (
        ToolCall(id="a", name="movie_details", arguments={"title": "Arrival"}),
        ToolCall(id="b", name="person_details", arguments={"name": "x"}),
        ToolCall(id="c", name="shell", arguments={}),
        ToolCall(id="d", name="web_search", arguments={"__unparseable__": "{"}),
        ToolCall(id="e", name="web_search", arguments={"query": "q"}),
    )
    results = []
    async for kind, value in t.run(calls):
        if kind == "results":
            results = value
    by_id = {call.id: result for call, result in results}
    assert "could not be reached" in by_id["a"]["error"] and t.upstream_failures == 1
    assert by_id["b"]["error"] == "This lookup failed; it could not be verified."
    assert by_id["c"]["error"] == "Unknown tool 'shell'."
    assert by_id["d"]["error"] == "The tool arguments were not valid JSON."
    assert "budget" in by_id["e"]["error"]
    assert t.saw_tool_results is True


@pytest.mark.asyncio
async def test_a_write_that_fails_says_nothing_was_changed(monkeypatch):
    t = tools()

    async def tmdb_down(args):
        raise UpstreamUnavailable("down", provider="tmdb")
        yield  # pragma: no cover

    async def database_error(args):
        raise RuntimeError("db")
        yield  # pragma: no cover

    for writer, expected in (
        (tmdb_down, "the movie database (TMDB) could not be reached"),
        (database_error, "could not be made"),
    ):
        monkeypatch.setattr(t, "update_my_list", writer)
        results = []
        async for kind, value in t.run((ToolCall(id="w", name="update_my_list", arguments={}),)):
            if kind == "results":
                results = value
        assert expected in results[0][1]["error"] and "nothing was changed" in results[0][1]["error"]


@pytest.mark.asyncio
async def test_references_are_recorded_in_the_state_when_no_callback_is_given():
    t = tools()
    t._refer([{"tmdb_id": 329865, "title": "Arrival", "release_date": "2016-11-10"}, {"title": "no id"}])
    assert [m["title"] for m in t.state["movies"]] == ["Arrival"] and t.state["movies"][0]["year"] == 2016
    assert json.dumps(t.state)  # still serialisable
    await t.close()
    assert t.engine.closed is False  # an engine passed in belongs to the caller
