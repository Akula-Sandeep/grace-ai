# agent/planner.py
from app.config import settings

from .models import (
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionTask,
    InformationRequirement,
)

# A grounded web-search evidence lookup takes longer than the engine's
# generic tool timeout (settings.tool_timeout_seconds); without this
# override every evidence task was cut off well before Gemini's own,
# already-generous grounding timeout could fire, regardless of plan
# latency budget.
_EVIDENCE_TASK_TIMEOUT = settings.gemini_grounding_timeout_seconds + 3.0
# Exposed so callers that wrap the whole semantic-evidence plan in their
# own outer deadline (e.g. the dispatcher's re-rank step) can stay above
# it, instead of guessing a number that silently drifts out of sync.
SEMANTIC_EVIDENCE_PLAN_LATENCY_SECONDS = max(12.0, _EVIDENCE_TASK_TIMEOUT + 4.0)


class ExecutionPlanner:
    """Builds bounded, dependency-aware execution plans for composite requests.

    Plans are deterministic: the LLM never chooses tools. Each plan declares its
    tasks, their dependencies and its tool / web-search / latency budgets, and
    is run by ``ExecutionEngine`` with ``MovieTaskHandlers`` (product spec §5,
    §18, §31). Simple requests never reach the planner; they use direct paths.
    """

    def comparison_plan(
        self,
        titles: list[str],
        aspects: list[str],
        *,
        web_available: bool,
        max_films: int = 4,
    ) -> ExecutionPlan:
        """Composite plan for comparing several films (product spec §5, §18).

        All films resolve in one task; each film's structured details and each
        (film, aspect) evidence acquisition then run in parallel with explicit
        dependencies, under one tool/web/latency budget. Evidence tasks are
        optional: a missing source degrades the answer instead of failing it.
        """
        films = [title for title in dict.fromkeys(t.strip() for t in titles) if title][:max_films]
        tasks = [
            ExecutionTask(
                id="resolve_references",
                type="resolve_movie_references",
                input={"references": films},
                required=True,
            )
        ]
        for index, _title in enumerate(films):
            tasks.append(
                ExecutionTask(
                    id=f"details_{index}",
                    type="get_movie_details",
                    input={
                        "movie_id": {
                            "from_task": "resolve_references",
                            "path": f"references.{index}.resolved.id",
                        },
                        "knowledge_check": {"satisfied": False},
                    },
                    depends_on=["resolve_references"],
                    required=False,
                )
            )
            if not web_available:
                continue
            for aspect_index, aspect in enumerate(aspects):
                tasks.append(
                    ExecutionTask(
                        id=f"evidence_{index}_{aspect_index}",
                        type="acquire_movie_evidence",
                        input={
                            "movie_id": {"from_task": f"details_{index}", "path": "data.id"},
                            "title": {"from_task": f"details_{index}", "path": "data.title"},
                            "year": {"from_task": f"details_{index}", "path": "data.release_date"},
                            "aspect": aspect,
                        },
                        depends_on=[f"details_{index}"],
                        required=False,
                        timeout_seconds=_EVIDENCE_TASK_TIMEOUT,
                    )
                )
        evidence_tasks = sum(1 for task in tasks if task.type == "acquire_movie_evidence")
        return ExecutionPlan(
            policy=ExecutionPolicy(
                path="deep",
                workload="deep" if evidence_tasks else "moderate",
                reasoning_level="medium",
                evidence_level="research" if evidence_tasks else "structured",
                knowledge_mode="research" if evidence_tasks else "structured",
            ),
            tasks=tasks,
            max_llm_calls=1,
            max_tool_calls=1 + 2 * len(films),
            max_web_searches=evidence_tasks,
            max_latency_seconds=25.0,
            max_iterations=1,
            stopping_conditions=["all_tasks_finished", "latency_budget_exhausted"],
        )

    def semantic_evidence_plan(
        self,
        candidates: list[dict],
        requirements: list[InformationRequirement],
        *,
        max_candidates: int = 4,
    ) -> ExecutionPlan:
        """Evidence-based re-ranking of already verified recommendation candidates.

        One evidence task per (candidate, soft requirement) runs in parallel, then
        a single ranking task interprets the evidence and orders the candidates.
        Hard constraints are never re-checked here: candidates are pre-verified.
        """
        pool = [c for c in candidates if c.get("id") and c.get("title")][:max_candidates]
        tasks: list[ExecutionTask] = []
        for c_index, candidate in enumerate(pool):
            for r_index, requirement in enumerate(requirements):
                tasks.append(
                    ExecutionTask(
                        id=f"evidence_{c_index}_{r_index}",
                        type="acquire_movie_evidence",
                        input={
                            "movie_id": candidate["id"],
                            "title": candidate["title"],
                            "year": candidate.get("release_date"),
                            "aspect": requirement.dimensions[0],
                        },
                        required=False,
                        timeout_seconds=_EVIDENCE_TASK_TIMEOUT,
                    )
                )
        tasks.append(
            ExecutionTask(
                id="rank_with_evidence",
                type="rank_with_evidence",
                input={
                    "candidates": pool,
                    "requirements": [r.model_dump() for r in requirements],
                },
                depends_on=[],
                required=True,
            )
        )
        # The ranking task must wait for evidence even when some evidence tasks fail,
        # so it runs as an adaptive task after the first (parallel) round.
        tasks[-1] = tasks[-1].model_copy(update={"adaptive": True})
        return ExecutionPlan(
            policy=ExecutionPolicy(
                path="targeted",
                workload="moderate",
                reasoning_level="low",
                evidence_level="qualitative",
                knowledge_mode="research",
            ),
            information_requirements=[
                InformationRequirement(
                    id="semantic_ranking",
                    type="structured",
                    subject="recommendation candidates",
                    fields=["rank_with_evidence"],
                    required=True,
                    acquisition_required=False,
                )
            ],
            tasks=tasks,
            max_llm_calls=len(requirements),
            max_tool_calls=0,
            max_web_searches=len(tasks) - 1,
            max_latency_seconds=SEMANTIC_EVIDENCE_PLAN_LATENCY_SECONDS,
            max_iterations=2,
            stopping_conditions=["ranking_complete", "latency_budget_exhausted"],
        )
