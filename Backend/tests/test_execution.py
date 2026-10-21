# tests/test_execution.py
import asyncio
import time

import pytest

from app.agent.execution import ExecutionEngine
from app.agent.models import (
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionTask,
)
from app.knowledge.evidence import Evidence


async def fake_task(
    task,
    state,
):
    await asyncio.sleep(0.2)
    return task.id


@pytest.mark.asyncio
async def test_independent_tasks_run_in_parallel():
    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="deep",
            reasoning_level="high",
            evidence_level="research",
            adaptive=True,
        ),
        tasks=[
            ExecutionTask(
                id="task_a",
                type="fake",
            ),
            ExecutionTask(
                id="task_b",
                type="fake",
            ),
        ],
        max_latency_seconds=5,
    )

    engine = ExecutionEngine(
        handlers={
            "fake": fake_task,
        }
    )

    started = time.perf_counter()

    result = await engine.execute(plan)

    elapsed = time.perf_counter() - started

    assert result.status == "complete"

    assert set(result.data.keys()) == {
        "task_a",
        "task_b",
    }

    # Two 200ms tasks should take roughly 200ms,
    # not roughly 400ms.
    assert elapsed < 0.35


@pytest.mark.asyncio
async def test_engine_centrally_accumulates_handler_evidence_once():
    evidence = Evidence(
        id="e1",
        movie_id=1,
        source_type="web",
        source_name="test",
        aspect="visual_darkness",
        value="dark",
        content="test evidence",
        confidence="high",
    )

    async def evidence_task(task, state):
        return {
            "evidence": [evidence],
        }

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="deep",
            reasoning_level="high",
            evidence_level="research",
        ),
        tasks=[
            ExecutionTask(
                id="evidence_task",
                type="evidence",
            ),
        ],
        max_latency_seconds=5,
    )

    engine = ExecutionEngine(
        handlers={
            "evidence": evidence_task,
        }
    )

    result = await engine.execute(plan)

    assert result.status == "complete"
    assert len(result.evidence) == 1
    assert result.evidence[0] is evidence


@pytest.mark.asyncio
async def test_execution_engine_tracks_tool_call_budget():
    async def tool_task(task, state):
        state.consume_tool_call()
        return {
            "status": "ok",
        }

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            reasoning_level="medium",
            evidence_level="structured",
        ),
        tasks=[
            ExecutionTask(
                id="tool",
                type="tool",
                required=True,
            )
        ],
        max_tool_calls=1,
    )

    engine = ExecutionEngine(
        handlers={
            "tool": tool_task,
        }
    )

    result = await engine.execute(plan)

    assert result.status == "complete"


@pytest.mark.asyncio
async def test_task_timeout_uses_plan_deadline_not_hard_1_5_seconds():
    async def slow_task(task, state):
        await asyncio.sleep(1.7)
        return {"status": "ok"}

    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="targeted", reasoning_level="medium", evidence_level="structured"),
        tasks=[ExecutionTask(id="slow", type="slow", timeout_seconds=3.0, required=True)],
        max_latency_seconds=3.0,
    )

    result = await ExecutionEngine(handlers={"slow": slow_task}).execute(plan)

    assert result.status == "complete"
    assert result.data["slow"]["status"] == "ok"


@pytest.mark.asyncio
async def test_task_timeout_seconds_can_exceed_the_default_tool_timeout(monkeypatch):
    """A task may explicitly ask for *more* time than settings.tool_timeout_
    seconds (e.g. a grounded web-search evidence task); the engine must honor
    that override instead of silently clamping it back down to the default.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "tool_timeout_seconds", 1.0)

    async def slower_than_default(task, state):
        await asyncio.sleep(1.5)
        return {"status": "ok"}

    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="targeted", reasoning_level="medium", evidence_level="structured"),
        tasks=[
            ExecutionTask(
                id="evidence",
                type="slow",
                timeout_seconds=5.0,
                required=True,
            )
        ],
        max_latency_seconds=10.0,
    )

    result = await ExecutionEngine(handlers={"slow": slower_than_default}).execute(plan)

    assert result.status == "complete"
    assert result.data["evidence"]["status"] == "ok"


@pytest.mark.asyncio
async def test_task_timeout_seconds_is_still_bounded_by_the_plan_deadline(monkeypatch):
    """A long per-task timeout_seconds is still capped by the plan's
    remaining deadline, which stays the hard ceiling."""
    from app.config import settings

    monkeypatch.setattr(settings, "tool_timeout_seconds", 1.0)

    async def too_slow(task, state):
        await asyncio.sleep(5.0)
        return {"status": "ok"}

    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="targeted", reasoning_level="medium", evidence_level="structured"),
        tasks=[
            ExecutionTask(
                id="evidence",
                type="slow",
                timeout_seconds=30.0,
                required=False,
            )
        ],
        max_latency_seconds=0.3,
    )

    result = await ExecutionEngine(handlers={"slow": too_slow}).execute(plan)

    assert result.data["evidence"]["status"] == "failed"
