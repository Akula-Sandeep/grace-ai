import pytest

from app.agent.models import ExecutionTask
from app.agent.task_handlers import MovieTaskHandlers


class FakeSearchTools:
    async def search_movies(self, query):
        return {
            "results": [
                {"id": 800158, "title": "The Killer", "release_date": "2023-10-25"},
                {"id": 970347, "title": "The Killer", "release_date": "2024-11-05"},
                {"id": 10835, "title": "The Killer", "release_date": "1989-09-15"},
            ]
        }


class FakeExactTools:
    async def search_movies(self, query):
        return {
            "results": [
                {"id": 999, "title": "A Different Movie"},
                {"id": 123, "title": "Inception"},
            ]
        }


@pytest.mark.asyncio
async def test_reference_resolution_does_not_assume_first_candidate():
    handlers = MovieTaskHandlers(tools=FakeExactTools())
    from app.agent.models import ExecutionPlan, ExecutionPolicy, ExecutionState

    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="fast"),
        max_tool_calls=2,
    )
    state = ExecutionState(plan=plan)

    result = await handlers.resolve_movie_references(
        ExecutionTask(
            id="resolve",
            type="resolve_movie_references",
            input={"references": ["Inception"]},
        ),
        state,
    )

    assert result["references"][0]["resolved"]["id"] == 123
    assert result["references"][0]["status"] == "resolved"


@pytest.mark.asyncio
async def test_reference_resolution_marks_same_title_candidates_ambiguous():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())
    # Use a tiny real state because the handler consumes the tool budget.
    from app.agent.models import ExecutionPlan, ExecutionPolicy, ExecutionState

    plan = ExecutionPlan(policy=ExecutionPolicy(path="fast"))
    state = ExecutionState(plan=plan)

    result = await handlers.resolve_movie_references(
        ExecutionTask(
            id="resolve",
            type="resolve_movie_references",
            input={"references": ["The Killer"]},
        ),
        state,
    )

    assert result["references"][0]["resolved"] is None
    assert result["references"][0]["status"] == "ambiguous"


def test_reference_resolution_uses_year_to_distinguish_same_title_candidates():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    ordered, resolved, confidence, status = handlers._resolve_candidate(
        "The Killer 1989",
        [
            {
                "id": 800158,
                "title": "The Killer",
                "release_date": "2023-10-25",
            },
            {
                "id": 970347,
                "title": "The Killer",
                "release_date": "2024-11-05",
            },
            {
                "id": 10835,
                "title": "The Killer",
                "release_date": "1989-09-15",
            },
        ],
    )

    assert resolved["id"] == 10835
    assert status == "resolved"
    assert 0.0 <= confidence <= 1.0
    assert ordered[0]["id"] == 10835


def test_reference_resolution_does_not_ignore_conflicting_year():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    _, resolved, _, status = handlers._resolve_candidate(
        "The Killer 2024",
        [
            {
                "id": 800158,
                "title": "The Killer",
                "release_date": "2023-10-25",
            },
            {
                "id": 970347,
                "title": "The Killer",
                "release_date": "2024-11-05",
            },
            {
                "id": 10835,
                "title": "The Killer",
                "release_date": "1989-09-15",
            },
        ],
    )

    assert resolved["id"] == 970347
    assert status == "resolved"


def test_reference_resolution_does_not_resolve_year_only_match():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    _, resolved, _, status = handlers._resolve_candidate(
        "Completely Different Movie 1989",
        [
            {
                "id": 800158,
                "title": "The Killer",
                "release_date": "2023-10-25",
            },
            {
                "id": 970347,
                "title": "The Killer",
                "release_date": "2024-11-05",
            },
            {
                "id": 10835,
                "title": "The Killer",
                "release_date": "1989-09-15",
            },
        ],
    )

    assert resolved is None
    assert status == "unresolved"


def test_reference_resolution_matches_original_title():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    _, resolved, _, status = handlers._resolve_candidate(
        "Parasite",
        [
            {
                "id": 1,
                "title": "기생충",
                "original_title": "Parasite",
                "release_date": "2019-05-30",
            },
            {
                "id": 2,
                "title": "Another Movie",
                "original_title": "Another Movie",
                "release_date": "2020-01-01",
            },
        ],
    )

    assert resolved["id"] == 1
    assert status == "resolved"


def test_reference_resolution_preserves_non_latin_titles():
    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    _, resolved, _, status = handlers._resolve_candidate(
        "기생충",
        [
            {
                "id": 496243,
                "title": "기생충",
                "original_title": "기생충",
                "release_date": "2019-05-30",
            },
            {
                "id": 2,
                "title": "Another Movie",
                "original_title": "Another Movie",
                "release_date": "2020-01-01",
            },
        ],
    )

    assert resolved["id"] == 496243
    assert status == "resolved"


@pytest.mark.asyncio
async def test_execution_does_not_treat_resolver_ambiguity_as_success():
    from app.agent.execution import ExecutionEngine
    from app.agent.models import (
        ExecutionPlan,
        ExecutionPolicy,
        InformationRequirement,
    )

    handlers = MovieTaskHandlers(tools=FakeSearchTools())

    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="fast"),
        information_requirements=[
            InformationRequirement(
                id="movie",
                type="structured",
                subject="The Killer",
                required=True,
            )
        ],
        tasks=[
            ExecutionTask(
                id="resolve_references",
                type="resolve_movie_references",
                input={"references": ["The Killer"]},
            ),
            ExecutionTask(
                id="get_movie_details",
                type="get_movie_details",
                input={
                    "movie_id": {
                        "from_task": "resolve_references",
                        "path": "references.0.resolved.id",
                    }
                },
                depends_on=["resolve_references"],
            ),
        ],
    )

    engine = ExecutionEngine(
        handlers={
            "resolve_movie_references": handlers.resolve_movie_references,
            "get_movie_details": handlers.get_movie_details,
        }
    )

    result = await engine.execute(plan)

    assert result.status != "complete"
    assert result.status in {"failed", "partial"}
