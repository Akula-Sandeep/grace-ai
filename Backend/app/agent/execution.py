# execution.py

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.observability import log_fallback

from .completeness import ExecutionCompleteness
from .models import (
    ExecutionPlan,
    ExecutionResult,
    ExecutionState,
    ExecutionTask,
)

TaskHandler = Callable[
    [ExecutionTask, ExecutionState],
    Awaitable[Any],
]


class ExecutionEngine:
    """
    Dependency-aware asynchronous execution engine.

    The engine knows how to schedule tasks.
    It does NOT know what TMDB or any other external service is.

    Domain-specific behavior is supplied through task handlers.
    """

    def __init__(
        self,
        handlers: dict[str, TaskHandler],
        completeness: ExecutionCompleteness | None = None,
    ) -> None:
        self.handlers = handlers
        self.completeness = completeness or ExecutionCompleteness()

    def _resolve_task_input(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> dict[str, Any]:
        return self._resolve_value(
            task.input,
            state,
        )

    def _resolve_value(
        self,
        value: Any,
        state: ExecutionState,
    ) -> Any:
        if isinstance(value, dict):
            if set(value.keys()) == {"from_task", "path"}:
                return self._get_result_value(
                    from_task=value["from_task"],
                    path=value["path"],
                    state=state,
                )

            return {
                key: self._resolve_value(
                    item,
                    state,
                )
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [
                self._resolve_value(
                    item,
                    state,
                )
                for item in value
            ]

        return value

    def _get_result_value(
        self,
        from_task: str,
        path: str,
        state: ExecutionState,
    ) -> Any:
        if from_task not in state.results:
            raise RuntimeError(f"Result for task '{from_task}' " f"is not available.")

        value = state.results[from_task]

        if not path:
            return value

        for part in path.split("."):
            if isinstance(value, dict):
                if part not in value:
                    raise KeyError(f"Path '{path}' not found in " f"result of task '{from_task}'.")

                value = value[part]

            elif isinstance(value, list):
                try:
                    value = value[int(part)]
                except (
                    ValueError,
                    IndexError,
                ) as exc:
                    raise KeyError(f"Path '{path}' not found in " f"result of task '{from_task}'.") from exc

            else:
                raise KeyError(f"Cannot continue path '{path}' " f"through value from task '{from_task}'.")

        return value

    def _evaluate_completeness(
        self,
        plan: ExecutionPlan,
        state: ExecutionState,
    ) -> tuple[bool, list[str]]:
        complete, unresolved = self.completeness.evaluate(
            plan,
            state,
        )

        state.unresolved_requirements = unresolved

        return complete, unresolved

    async def execute(
        self,
        plan: ExecutionPlan,
    ) -> ExecutionResult:
        state = ExecutionState(
            plan=plan,
            pending_tasks=[task.id for task in plan.tasks],
            current_path=plan.policy.path,
        )

        started_at = time.perf_counter()
        state.started_at = started_at
        state.deadline_at = started_at + max(0.1, plan.max_latency_seconds)

        try:
            while state.pending_tasks:
                ready_tasks = self._get_ready_tasks(
                    plan,
                    state,
                    include_adaptive=False,
                )

                if ready_tasks:
                    await self._run_ready_tasks(
                        ready_tasks,
                        state,
                    )

                    state.iteration += 1

                    if time.perf_counter() >= (state.deadline_at or float("inf")):
                        _, unresolved = self._evaluate_completeness(
                            plan,
                            state,
                        )
                        return ExecutionResult(
                            status="partial",
                            data=state.results,
                            evidence=state.evidence,
                            unresolved_requirements=unresolved,
                            termination_reason=("Execution exceeded the configured " "latency budget."),
                        )

                    continue

                complete, unresolved = self._evaluate_completeness(
                    plan,
                    state,
                )

                if complete:
                    break

                if state.iteration >= plan.max_iterations:
                    return ExecutionResult(
                        status="partial",
                        data=state.results,
                        evidence=state.evidence,
                        unresolved_requirements=unresolved,
                        termination_reason=(
                            "Adaptive execution reached its iteration budget "
                            "before all required information was satisfied."
                        ),
                    )

                adaptive_tasks = self._get_ready_tasks(
                    plan,
                    state,
                    include_adaptive=True,
                )

                if not adaptive_tasks:
                    return ExecutionResult(
                        status="partial",
                        data=state.results,
                        evidence=state.evidence,
                        unresolved_requirements=unresolved,
                        termination_reason=(
                            "No remaining adaptive task can satisfy the "
                            "unresolved information requirements."
                        ),
                    )

                await self._run_ready_tasks(
                    adaptive_tasks,
                    state,
                )

                state.iteration += 1

                if time.perf_counter() >= (state.deadline_at or float("inf")):
                    return ExecutionResult(
                        status="partial",
                        data=state.results,
                        evidence=state.evidence,
                        unresolved_requirements=(state.unresolved_requirements),
                        termination_reason=("Adaptive execution exceeded the configured " "latency budget."),
                    )

            if plan.policy.path == "clarify":
                return ExecutionResult(
                    status="clarification_required",
                    data=state.results,
                    evidence=state.evidence,
                    termination_reason=("Clarification is required before execution."),
                )

            complete, unresolved = self._evaluate_completeness(
                plan,
                state,
            )

            if complete:
                return ExecutionResult(
                    status="complete",
                    data=state.results,
                    evidence=state.evidence,
                    termination_reason=("All required information requirements " "are satisfied."),
                )

            return ExecutionResult(
                status="partial",
                data=state.results,
                evidence=state.evidence,
                unresolved_requirements=unresolved,
                termination_reason=(
                    "All executable tasks completed, but one or more "
                    "required information requirements remain unsatisfied."
                ),
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log_fallback("execution_fallback", "task_execution", exc)
            return ExecutionResult(
                status="failed",
                data=state.results,
                evidence=state.evidence,
                termination_reason=str(exc),
            )

    def _get_ready_tasks(
        self,
        plan: ExecutionPlan,
        state: ExecutionState,
        include_adaptive: bool = False,
    ) -> list[ExecutionTask]:
        completed = set(state.completed_tasks)

        running = set(state.running_tasks)

        ready: list[ExecutionTask] = []

        for task in plan.tasks:
            if task.id not in state.pending_tasks:
                continue

            if task.id in running:
                continue

            if task.adaptive and not include_adaptive:
                continue

            dependencies_satisfied = all(dependency in completed for dependency in task.depends_on)

            if dependencies_satisfied:
                ready.append(task)

        return ready

    async def _run_ready_tasks(
        self,
        tasks: list[ExecutionTask],
        state: ExecutionState,
    ) -> None:
        """
        Execute every currently-ready task concurrently.

        Successful task results are stored in execution state.
        Evidence returned by a task is accumulated centrally.
        Optional task failures are recorded without failing the
        overall execution.
        """

        for task in tasks:
            state.pending_tasks.remove(task.id)
            state.running_tasks.append(task.id)

        results = await asyncio.gather(
            *[self._run_task(task, state) for task in tasks],
            return_exceptions=True,
        )

        for task, result in zip(
            tasks,
            results,
            strict=True,
        ):
            state.running_tasks.remove(task.id)

            if isinstance(result, Exception):
                state.failed_tasks.append(task.id)

                if task.required:
                    raise result

                state.results[task.id] = {
                    "error": str(result),
                    "status": "failed",
                }

                continue

            state.results[task.id] = result
            state.completed_tasks.append(task.id)

            if isinstance(result, dict):
                evidence = result.get("evidence")

                if evidence:
                    state.evidence.extend(evidence)

    async def _run_task(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> Any:
        handler = self.handlers.get(task.type)

        if handler is None:
            raise RuntimeError(f"No handler registered for task type: " f"{task.type}")

        resolved_input = self._resolve_task_input(
            task,
            state,
        )

        execution_task = task.model_copy(
            update={
                "input": resolved_input,
            }
        )

        remaining = float("inf")
        if state.deadline_at is not None:
            remaining = state.deadline_at - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("Execution deadline exceeded before task start.")

        # A task's own ``timeout_seconds`` is an override, not just a tighter
        # cap: it may legitimately ask for *more* than the default tool
        # timeout (a grounded web search takes longer than a structured
        # lookup). The plan's remaining deadline is still the hard ceiling.
        timeout = min(task.timeout_seconds or settings.tool_timeout_seconds, remaining)
        return await asyncio.wait_for(
            handler(
                execution_task,
                state,
            ),
            timeout=max(0.1, timeout),
        )
