import asyncio
import time

import pytest

from app.agent.models import (
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionState,
    ExecutionTask,
)
from app.agent.task_handlers import MovieTaskHandlers
from app.knowledge import (
    InMemoryMovieKnowledgeStore,
)


class FakeMovieTools:
    def __init__(self):
        self.search_calls = []
        self.details_calls = []

    def test_tool_call_budget_is_enforced():
        plan = ExecutionPlan(
            policy=ExecutionPolicy(
                path="targeted",
                reasoning_level="medium",
                evidence_level="structured",
                adaptive=True,
            ),
            max_tool_calls=1,
        )

        state = ExecutionState(plan=plan)

        state.consume_tool_call()

        assert state.tool_calls == 1

        with pytest.raises(RuntimeError, match="Tool call budget exceeded"):
            state.consume_tool_call()

        assert state.tool_calls == 1

    async def search_movies(self, query):
        self.search_calls.append(query)

        return {
            "query": query,
            "results": [
                {
                    "id": 123,
                    "title": "Test Movie",
                }
            ],
        }

    async def discover_movies(self, **kwargs):
        return {
            "results": [
                {
                    "id": 456,
                    "title": "Another Movie",
                }
            ]
        }

    async def get_movie_details(self, movie_id):
        self.details_calls.append(movie_id)

        return {
            "id": movie_id,
            "title": "Test Movie",
            "runtime": 120,
        }


class FakeAcquisitionOrchestrator:
    def __init__(self):
        self.calls = []

    async def acquire(
        self,
        requirement,
        check,
        movie_id=None,
        query=None,
    ):
        self.calls.append(
            {
                "requirement": requirement,
                "check": check,
                "movie_id": movie_id,
                "query": query,
            }
        )

        return None


@pytest.mark.asyncio
async def test_resolve_movie_references():
    handlers = MovieTaskHandlers(tools=FakeMovieTools())

    task = ExecutionTask(
        id="resolve",
        type="resolve_movie_references",
        input={"references": ["war2"]},
    )

    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(
                path="fast",
                reasoning_level="none",
                evidence_level="structured",
            )
        )
    )

    result = await handlers.resolve_movie_references(
        task,
        state,
    )

    assert result["references"][0]["reference"] == "war2"
    assert result["references"][0]["candidates"][0]["title"] == "Test Movie"
    assert state.tool_calls == 1


@pytest.mark.asyncio
async def test_reference_resolution_runs_in_parallel():
    class SlowFakeMovieTools:
        async def search_movies(self, query):
            await asyncio.sleep(0.2)

            return {
                "query": query,
                "results": [
                    {
                        "id": 123,
                        "title": query,
                    }
                ],
            }

    handlers = MovieTaskHandlers(tools=SlowFakeMovieTools())

    task = ExecutionTask(
        id="resolve",
        type="resolve_movie_references",
        input={
            "references": [
                "Movie A",
                "Movie B",
                "Movie C",
                "Movie D",
                "Movie E",
            ]
        },
    )

    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(
                path="deep",
                reasoning_level="high",
                evidence_level="research",
            )
        )
    )

    started = time.perf_counter()

    result = await handlers.resolve_movie_references(
        task,
        state,
    )

    elapsed = time.perf_counter() - started

    assert result["references"]
    assert len(result["references"]) == 5

    # Five independent 200ms operations should run concurrently,
    # so total execution should remain well below 1 second.
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_get_movie_details_uses_knowledge_on_hit():
    fake_tools = FakeMovieTools()

    handlers = MovieTaskHandlers(
        tools=fake_tools,
    )

    task = ExecutionTask(
        id="get_movie_details",
        type="get_movie_details",
        input={
            "movie_id": 123,
            "knowledge_check": {
                "satisfied": True,
                "knowledge": {
                    "movie_id": 123,
                    "title": "Test Movie",
                    "runtime": 120,
                },
            },
        },
    )

    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(
                path="fast",
                reasoning_level="none",
                evidence_level="structured",
            )
        )
    )

    result = await handlers.get_movie_details(
        task,
        state,
    )

    assert result["source"] == "knowledge"
    assert result["data"]["runtime"] == 120
    assert fake_tools.details_calls == []
    assert state.tool_calls == 0


@pytest.mark.asyncio
async def test_get_movie_details_uses_tmdb_on_miss():
    fake_tools = FakeMovieTools()

    handlers = MovieTaskHandlers(
        tools=fake_tools,
    )

    task = ExecutionTask(
        id="get_movie_details",
        type="get_movie_details",
        input={
            "movie_id": 123,
            "knowledge_check": {
                "satisfied": False,
                "missing_fields": ["runtime"],
            },
        },
    )

    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(
                path="fast",
                reasoning_level="none",
                evidence_level="structured",
            )
        )
    )

    result = await handlers.get_movie_details(
        task,
        state,
    )

    assert result["source"] == "tmdb"
    assert result["data"]["runtime"] == 120
    assert fake_tools.details_calls == [123]
    assert state.tool_calls == 1


@pytest.mark.asyncio
async def test_tmdb_result_is_saved_and_reused():
    fake_tools = FakeMovieTools()

    store = InMemoryMovieKnowledgeStore()

    handlers = MovieTaskHandlers(
        tools=fake_tools,
        knowledge_store=store,
    )

    miss_task = ExecutionTask(
        id="get_movie_details",
        type="get_movie_details",
        input={
            "movie_id": 123,
            "knowledge_check": {
                "satisfied": False,
                "missing_fields": ["runtime"],
            },
        },
    )

    state = ExecutionState(
        plan=ExecutionPlan(
            policy=ExecutionPolicy(
                path="fast",
                reasoning_level="none",
                evidence_level="structured",
            )
        )
    )

    first_result = await handlers.get_movie_details(
        miss_task,
        state,
    )

    assert first_result["source"] == "tmdb"
    assert fake_tools.details_calls == [123]
    assert state.tool_calls == 1

    stored = await store.get_by_id(123)

    assert stored is not None
    assert stored.runtime == 120

    hit_task = ExecutionTask(
        id="get_movie_details",
        type="get_movie_details",
        input={
            "movie_id": 123,
            "knowledge_check": {
                "satisfied": True,
                "knowledge": stored.model_dump(),
            },
        },
    )

    second_result = await handlers.get_movie_details(
        hit_task,
        state,
    )

    assert second_result["source"] == "knowledge"
    assert second_result["data"]["runtime"] == 120
    assert fake_tools.details_calls == [123]
