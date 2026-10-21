import asyncio

import pytest

from app.agent.budget import (
    ToolBudget,
    bind_tool_budget,
    budgeted_leaf,
    current_tool_budget,
    tool_budget_scope,
)
from app.agent.tools import MovieTools


@pytest.mark.asyncio
async def test_nested_leaf_operations_share_the_same_budget_and_concurrency_gate():
    budget = ToolBudget(max_calls=5, max_concurrency=2)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def leaf():
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return "ok"

    async def composite():
        async def run_leaf():
            async with budgeted_leaf():
                return await leaf()

        return await asyncio.gather(*(run_leaf() for _ in range(5)))

    with bind_tool_budget(budget):
        results = await composite()

    assert results == ["ok"] * 5
    assert budget.calls == 5
    assert peak <= 2


@pytest.mark.asyncio
async def test_nested_leaf_budget_rejects_excess_fanout_before_external_calls():
    budget = ToolBudget(max_calls=2, max_concurrency=2)
    entered = 0

    async def leaf():
        nonlocal entered
        entered += 1
        return "ok"

    async def run_leaf():
        async with budgeted_leaf():
            return await leaf()

    with bind_tool_budget(budget):
        results = await asyncio.gather(*(run_leaf() for _ in range(4)), return_exceptions=True)

    assert [result for result in results if result == "ok"] == ["ok", "ok"]
    assert sum(isinstance(result, RuntimeError) for result in results) == 2
    assert entered == 2
    assert budget.calls == 2


@pytest.mark.asyncio
async def test_movie_intelligence_counts_nested_provider_and_web_leaves():
    class FakeWeb:
        def __init__(self):
            self.active = 0
            self.peak = 0
            self.calls = 0
            self.lock = asyncio.Lock()

        async def search(self, query):
            async with self.lock:
                self.active += 1
                self.calls += 1
                self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.005)
            async with self.lock:
                self.active -= 1
            return {
                "results": [
                    {
                        "title": "Supported source",
                        "url": "https://example.com",
                        "snippet": query,
                    }
                ]
            }

    class FakeWikidata:
        async def awards(self, title, year):
            return []

    tools = MovieTools.__new__(MovieTools)
    tools.db = None
    tools.db_factory = None
    tools.web = FakeWeb()
    tools.wikidata = FakeWikidata()

    async def fake_details(movie_id, fresh=False):
        return {"id": movie_id, "title": "Example Movie", "release_date": "2024-01-01"}

    async def fake_persist(*args, **kwargs):
        return None

    async def fake_synthesize(*args, **kwargs):
        return {"facts": []}

    tools.get_movie_details = fake_details
    tools._persist_research = fake_persist
    tools._synthesize_intelligence = fake_synthesize
    tools._research_plan = lambda title, focus: [
        {"domain": "facts", "query": "facts"},
        {"domain": "music", "query": "music"},
    ]

    budget = ToolBudget(max_calls=4, max_concurrency=2)
    with bind_tool_budget(budget):
        result = await tools.movie_intelligence(1, web_budget=2)

    assert result["web_search_count"] == 2
    assert tools.web.calls == 2
    assert budget.calls == 4  # movie details + Wikidata + two web searches
    assert tools.web.peak <= 2


@pytest.mark.asyncio
async def test_movie_intelligence_stops_extra_web_leaves_at_budget_boundary():
    class FakeWeb:
        def __init__(self):
            self.calls = 0

        async def search(self, query):
            self.calls += 1
            return {"results": []}

    class FakeWikidata:
        async def awards(self, title, year):
            return []

    tools = MovieTools.__new__(MovieTools)
    tools.db = None
    tools.db_factory = None
    tools.web = FakeWeb()
    tools.wikidata = FakeWikidata()

    async def fake_details(movie_id, fresh=False):
        return {"id": movie_id, "title": "Example Movie", "release_date": "2024-01-01"}

    tools.get_movie_details = fake_details
    tools._research_plan = lambda title, focus: [
        {"domain": "facts", "query": "facts"},
        {"domain": "music", "query": "music"},
    ]

    async def noop_persist(*args, **kwargs):
        return None

    async def noop_synthesize(*args, **kwargs):
        return {"facts": []}

    tools._persist_research = noop_persist
    tools._synthesize_intelligence = noop_synthesize

    budget = ToolBudget(max_calls=3, max_concurrency=2)
    with bind_tool_budget(budget):
        result = await tools.movie_intelligence(1, web_budget=2)

    assert result["web_search_count"] == 1
    assert tools.web.calls == 1
    assert budget.calls == 3


@pytest.mark.asyncio
async def test_local_movie_search_preserves_cached_popularity_when_upstream_omits_it():
    class FakeTMDB:
        async def search_movies(self, query, page):
            return {
                "total_results": 1,
                "results": [
                    {
                        "id": 123,
                        "title": "Example Movie",
                        "original_title": "Example Movie",
                        "release_date": "2024-01-01",
                        "popularity": None,
                    }
                ],
            }

    tools = MovieTools.__new__(MovieTools)
    tools.db = object()
    tools.db_factory = None
    tools.tmdb = FakeTMDB()

    async def fake_db_call(fn):
        return [
            {
                "id": 123,
                "title": "Example Movie",
                "original_title": "Example Movie",
                "release_date": "2024-01-01",
                "overview": None,
                "vote_average": 8.0,
                "vote_count": 100,
                "popularity": 42.0,
                "poster_path": None,
                "backdrop_path": None,
                "genre_ids": [],
                "_source": "local",
            }
        ]

    tools._db_call = fake_db_call

    result = await tools.search_movies("Example Movie")
    assert result["results"][0]["popularity"] == 42.0


@pytest.mark.asyncio
async def test_web_budget_rejection_is_atomic():
    budget = ToolBudget(max_calls=2, max_web_searches=1, max_concurrency=1)
    async with budgeted_leaf(budget, web=True):
        pass

    assert budget.calls == 1
    assert budget.web_searches == 1

    with pytest.raises(RuntimeError, match="Web-search budget exceeded"):
        async with budgeted_leaf(budget, web=True):
            pass

    assert budget.calls == 1
    assert budget.web_searches == 1


@pytest.mark.asyncio
async def test_tool_budget_web_limit_also_respects_global_call_limit():
    budget = ToolBudget(max_calls=1, max_web_searches=4, max_concurrency=1)
    async with budgeted_leaf(budget, web=True):
        pass

    with pytest.raises(RuntimeError, match="Tool budget exceeded"):
        async with budgeted_leaf(budget, web=True):
            pass

    assert budget.calls == 1
    assert budget.web_searches == 1


@pytest.mark.asyncio
async def test_public_composite_tool_creates_budget_when_called_outside_dispatcher():
    observed = {}

    @tool_budget_scope
    async def composite():
        observed["budget"] = current_tool_budget()
        async with budgeted_leaf():
            return "ok"

    assert await composite() == "ok"
    assert observed["budget"] is not None
    assert observed["budget"].calls == 1
    assert current_tool_budget() is None
