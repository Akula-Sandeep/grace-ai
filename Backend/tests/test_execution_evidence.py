# tests/test_execution_evidence.py
import pytest

from app.agent.execution import ExecutionEngine
from app.agent.models import (
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionTask,
)


def make_plan(tasks, **kwargs):
    return ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            reasoning_level="low",
            evidence_level="qualitative",
            adaptive=True,
        ),
        tasks=tasks,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_execution_collects_evidence_from_task_result():
    async def handler(task, state):
        return {
            "value": "dark",
            "evidence": [
                {
                    "id": "e1",
                    "aspect": "visual_darkness",
                }
            ],
        }

    engine = ExecutionEngine(
        handlers={
            "collect": handler,
        }
    )

    plan = make_plan(
        [
            ExecutionTask(
                id="collect",
                type="collect",
            )
        ]
    )

    result = await engine.execute(plan)

    assert result.status == "complete"
    assert result.evidence == [
        {
            "id": "e1",
            "aspect": "visual_darkness",
        }
    ]


@pytest.mark.asyncio
async def test_execution_preserves_task_result():
    async def handler(task, state):
        return {
            "value": "Inception",
        }

    engine = ExecutionEngine(
        handlers={
            "lookup": handler,
        }
    )

    plan = make_plan(
        [
            ExecutionTask(
                id="lookup",
                type="lookup",
            )
        ]
    )

    result = await engine.execute(plan)

    assert result.status == "complete"
    assert result.data["lookup"]["value"] == "Inception"


@pytest.mark.asyncio
async def test_optional_failed_task_does_not_fail_execution():
    async def handler(task, state):
        raise RuntimeError("temporary failure")

    engine = ExecutionEngine(
        handlers={
            "optional": handler,
        }
    )

    plan = make_plan(
        [
            ExecutionTask(
                id="optional",
                type="optional",
                required=False,
            )
        ]
    )

    result = await engine.execute(plan)

    assert result.status == "complete"
    assert "optional" in result.data or "optional" in result.unresolved_requirements


@pytest.mark.asyncio
async def test_required_failed_task_fails_execution():
    async def handler(task, state):
        raise RuntimeError("fatal failure")

    engine = ExecutionEngine(
        handlers={
            "required": handler,
        }
    )

    plan = make_plan(
        [
            ExecutionTask(
                id="required",
                type="required",
                required=True,
            )
        ]
    )

    result = await engine.execute(plan)

    assert result.status == "failed"
    assert "fatal failure" in result.termination_reason


@pytest.mark.asyncio
async def test_dependency_results_are_available_to_downstream_tasks():
    async def first(task, state):
        return {
            "movie_id": 27205,
        }

    async def second(task, state):
        assert task.input["movie_id"] == 27205

        return {
            "title": "Inception",
        }

    engine = ExecutionEngine(
        handlers={
            "first": first,
            "second": second,
        }
    )

    plan = make_plan(
        [
            ExecutionTask(
                id="first",
                type="first",
            ),
            ExecutionTask(
                id="second",
                type="second",
                depends_on=["first"],
                input={
                    "movie_id": {
                        "from_task": "first",
                        "path": "movie_id",
                    }
                },
            ),
        ]
    )

    result = await engine.execute(plan)

    assert result.status == "complete"
    assert result.data["second"]["title"] == "Inception"
