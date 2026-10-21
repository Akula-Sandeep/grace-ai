"""Web search (services/web_search.py): Gemini grounded search, then Groq browser search.

2026-10-21: grounded search was out of free quota, so "check the web" answered
"I couldn't reach a web source". A second searcher now takes over.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import web_search
from app.services.movie_data_fallback import MovieDataFallbackEngine
from app.services.upstream_errors import UpstreamUnavailable

GROUNDED = {"query": "q", "summary": "From Gemini.", "sources": ["https://a.example"], "source_details": []}


class FakeGroq:
    """Stands in for groq.AsyncGroq; records the request."""

    requests: list = []
    reply: object = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **request):
        FakeGroq.requests.append(request)
        if isinstance(FakeGroq.reply, BaseException):
            raise FakeGroq.reply
        return FakeGroq.reply

    async def close(self):
        return None


def browser_reply(content="Avengers: Doomsday opens on 18 December 2026【1†L2-L4】."):
    result = SimpleNamespace(
        url="https://www.marvel.com/movies/avengers-doomsday",
        title="Avengers: Doomsday",
        content="In theaters December 18, 2026.",
    )
    tool = SimpleNamespace(search_results=SimpleNamespace(results=[result, result]), browser_results=[])
    message = SimpleNamespace(content=content, executed_tools=[tool])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def providers(monkeypatch):
    import groq

    monkeypatch.setattr(settings, "gemini_api_key", "g")
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(groq, "AsyncGroq", FakeGroq)
    FakeGroq.requests, FakeGroq.reply = [], browser_reply()

    def grounded(outcome):
        async def fake(cls, query):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(MovieDataFallbackEngine, "grounded_search", classmethod(fake))

    return grounded


@pytest.mark.asyncio
async def test_gemini_answers_first(providers):
    providers(GROUNDED)
    assert await web_search.search("q") == GROUNDED
    assert FakeGroq.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("gemini", [UpstreamUnavailable("429 RESOURCE_EXHAUSTED", provider="gemini"), None])
async def test_when_gemini_fails_or_finds_nothing_groq_browser_search_answers(providers, gemini):
    providers(gemini)
    result = await web_search.search("Avengers Doomsday release date")
    assert result["summary"] == "Avengers: Doomsday opens on 18 December 2026."  # markers removed
    assert result["sources"] == ["https://www.marvel.com/movies/avengers-doomsday"]
    assert result["source_details"][0]["source_domain"] == "marvel.com"
    request = FakeGroq.requests[0]
    assert request["tools"] == [{"type": "browser_search"}] and request["model"] == "openai/gpt-oss-20b"
    assert request["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_when_every_searcher_fails_the_model_is_told_to_say_so(providers, monkeypatch):
    import httpx

    providers(UpstreamUnavailable("down", provider="gemini"))
    FakeGroq.reply = httpx.ConnectError("down")
    failed = await web_search.search("q")
    assert failed["status"] == "search_failed" and "offer to try again" in failed["note"]
    FakeGroq.reply = browser_reply(content="")
    assert (await web_search.search("q"))["status"] == "search_failed"
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    assert (await web_search.search("q"))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_a_non_gpt_oss_background_model_searches_with_gpt_oss(providers, monkeypatch):
    providers(None)
    monkeypatch.setattr(settings, "groq_model", "qwen/qwen3.8-27b")
    await web_search.search("q")
    assert FakeGroq.requests[0]["model"] == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_the_tool_and_the_research_client_use_the_same_search(providers):
    from app.agent import working_state as working
    from app.agent.model_tools import TurnTools
    from app.services.web_search_client import GeminiGroundedSearchProvider

    providers(None)
    tools = TurnTools(
        user_message="x", chat_history=[], state=working.empty_state(), router=None, movie_engine=object()
    )
    assert (await tools.web_search({"query": "Avengers Doomsday"}))["searched_with"] == "Groq browser search"
    assert await tools.web_search({"query": " "}) == {"status": "needs_query"}
    found = await GeminiGroundedSearchProvider().search("Avengers Doomsday")
    assert found["results"][0]["url"] == "https://www.marvel.com/movies/avengers-doomsday"
    FakeGroq.reply = browser_reply(content="")
    with pytest.raises(RuntimeError):
        await GeminiGroundedSearchProvider().search("nothing")


def test_the_model_is_told_todays_date_in_the_users_time_zone():
    from app.agent.context import today_line

    now = datetime(2026, 10, 21, 6, 36, tzinfo=UTC)
    assert (
        today_line("Asia/Kolkata", now)
        == "Today is Wednesday 21 October 2026, 12:06 in the user's time zone (Asia/Kolkata)."
    )
    assert "(UTC)" in today_line("Not/AZone", now)
