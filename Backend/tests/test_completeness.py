import pytest

from app.agent.completeness import ExecutionCompleteness
from app.agent.execution import ExecutionEngine
from app.agent.models import (
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionState,
    ExecutionTask,
    InformationRequirement,
)
from app.knowledge.evidence import Evidence


async def structured_task(task, state):
    return {"data": {"title": "Inception", "runtime": 148}}


async def insufficient_task(task, state):
    return {"data": {"title": "Inception"}}


async def no_op(task, state):
    return {"ok": True}


@pytest.mark.asyncio
async def test_execution_is_not_complete_when_required_information_is_missing():
    requirement = InformationRequirement(
        id="movie_runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )
    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="fast",
            reasoning_level="none",
            evidence_level="structured",
        ),
        information_requirements=[requirement],
        tasks=[ExecutionTask(id="lookup", type="lookup")],
    )

    result = await ExecutionEngine({"lookup": insufficient_task}).execute(plan)

    assert result.status == "partial"
    assert result.unresolved_requirements == ["movie_runtime"]


@pytest.mark.asyncio
async def test_execution_is_complete_only_when_required_information_is_satisfied():
    requirement = InformationRequirement(
        id="movie_runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )
    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="fast",
            reasoning_level="none",
            evidence_level="structured",
        ),
        information_requirements=[requirement],
        tasks=[ExecutionTask(id="lookup", type="lookup")],
    )

    result = await ExecutionEngine({"lookup": structured_task}).execute(plan)

    assert result.status == "complete"
    assert result.unresolved_requirements == []


@pytest.mark.asyncio
async def test_structured_requirement_is_satisfied_by_acquired_evidence():
    requirement = InformationRequirement(
        id="movie_runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            reasoning_level="medium",
            evidence_level="structured",
        ),
        information_requirements=[requirement],
    )

    state = ExecutionState(
        plan=plan,
        evidence=[
            Evidence(
                id="evidence-1",
                movie_id=27205,
                source_type="structured",
                source_name="tmdb",
                aspect="runtime",
                content="148",
                confidence="high",
                spoiler_level="none",
            )
        ],
    )

    complete = ExecutionCompleteness()

    satisfied, unresolved = complete.evaluate(
        plan,
        state,
    )

    assert satisfied is True
    assert unresolved == []


@pytest.mark.asyncio
async def test_clarification_plan_returns_clarification_required():
    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="clarify",
            reasoning_level="none",
            evidence_level="none",
        ),
    )

    result = await ExecutionEngine({}).execute(plan)

    assert result.status == "clarification_required"


def test_model_knowledge_requirement_is_satisfied_without_execution_evidence():
    requirement = InformationRequirement(
        id="model_knowledge_0",
        type="qualitative",
        subject="Inception",
        dimensions=["information_need"],
        description="Why is Inception regarded as significant?",
        knowledge_mode="model_knowledge",
        acquisition_required=False,
        evidence_required=False,
    )

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            workload="lightweight",
            reasoning_level="low",
            evidence_level="structured",
            knowledge_mode="model_knowledge",
        ),
        information_requirements=[requirement],
        tasks=[],
    )

    state = ExecutionState(
        plan=plan,
    )

    complete, unresolved = ExecutionCompleteness().evaluate(
        plan,
        state,
    )

    assert complete is True
    assert unresolved == []


def test_unrelated_available_result_does_not_satisfy_user_context_requirement():
    requirement = InformationRequirement(
        id="user_context",
        type="user_context",
        subject="user",
        required=True,
    )

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            reasoning_level="medium",
            evidence_level="structured",
        ),
        information_requirements=[requirement],
        tasks=[],
    )

    state = ExecutionState(
        plan=plan,
        results={
            "some_unrelated_task": {
                "status": "available",
            }
        },
    )

    complete, unresolved = ExecutionCompleteness().evaluate(
        plan,
        state,
    )

    assert complete is False
    assert unresolved == ["user_context"]


def test_optional_enrichment_does_not_block_core_metadata_completion():
    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="targeted"),
        information_requirements=[
            InformationRequirement(
                id="core_metadata",
                type="structured",
                subject="Inception",
                fields=["title", "director", "release_year"],
            ),
            InformationRequirement(
                id="soundtrack_singers",
                type="structured",
                subject="Inception",
                fields=["soundtrack_singers"],
            ),
        ],
    )
    state = ExecutionState(
        plan=plan,
        results={
            "movie_details": {
                "data": {
                    "title": "Inception",
                    "director": "Christopher Nolan",
                    "release_year": 2010,
                }
            }
        },
    )

    complete, unresolved = ExecutionCompleteness().evaluate(plan, state)

    assert complete is True
    assert unresolved == []
