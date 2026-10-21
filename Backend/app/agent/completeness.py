from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.knowledge.evidence import Evidence
from app.knowledge.evidence_resolver import EvidenceResolver
from app.observability import log_fallback

from .models import ExecutionPlan, ExecutionState, InformationRequirement


class ExecutionCompleteness:
    """Evaluate whether an execution has enough information to answer.

    This is deliberately separate from task completion. A plan can finish all
    of its tasks while one or more information requirements remain unsatisfied.
    """

    def __init__(self, evidence_resolver: EvidenceResolver | None = None) -> None:
        self.evidence_resolver = evidence_resolver or EvidenceResolver()

    def evaluate(
        self,
        plan: ExecutionPlan,
        state: ExecutionState,
    ) -> tuple[bool, list[str]]:
        unresolved: list[str] = []

        for requirement in plan.information_requirements:
            if not requirement.required or self._is_optional_enrichment(requirement):
                continue
            if not self._is_satisfied(requirement, state):
                unresolved.append(requirement.id)

        return not unresolved, unresolved

    @staticmethod
    def _is_optional_enrichment(requirement: InformationRequirement) -> bool:
        """Treat costly enrichment as best-effort, never a completion blocker."""
        searchable = " ".join(
            [
                requirement.id,
                requirement.description,
                *requirement.fields,
                *requirement.dimensions,
            ]
        ).casefold()
        enrichment_terms = (
            "award",
            "filming",
            "location",
            "coordinate",
            "soundtrack",
            "singer",
            "vocal",
        )
        return any(term in searchable for term in enrichment_terms)

    def _is_satisfied(
        self,
        requirement: InformationRequirement,
        state: ExecutionState,
    ) -> bool:
        if requirement.knowledge_mode == "model_knowledge":
            return True
        if requirement.type in {"qualitative", "research", "current"}:
            # Recommendation plans evaluate semantic requirements at the
            # candidate level. Global evidence matching is insufficient
            # because evidence for one movie must not satisfy a requirement
            # for every other movie. Prefer the explicit ranking result when
            # it exists.
            ranking_result = state.results.get("rank_candidates")
            if isinstance(ranking_result, dict):
                requirement_evaluation = ranking_result.get("requirement_evaluation", {})
                evaluation = requirement_evaluation.get(requirement.id)
                if isinstance(evaluation, dict):
                    return (
                        evaluation.get(
                            "covered_candidate_count",
                            0,
                        )
                        > 0
                    )
                return False

            return self._evidence_satisfies(requirement, state)

        if requirement.type == "structured":
            return self._structured_satisfied(requirement, state)

        if requirement.type == "user_context":
            return self._user_context_satisfied(requirement, state)

        return False

    def _evidence_satisfies(
        self,
        requirement: InformationRequirement,
        state: ExecutionState,
    ) -> bool:
        evidence = self._coerce_evidence(state.evidence)
        if not evidence:
            return False

        result = self.evidence_resolver.check(requirement, evidence)
        return bool(result["satisfied"])

    def _structured_satisfied(
        self,
        requirement: InformationRequirement,
        state: ExecutionState,
    ) -> bool:
        # First check evidence acquired during execution.
        evidence = self._coerce_evidence(state.evidence)

        if evidence:
            result = self.evidence_resolver.check(
                requirement,
                evidence,
            )

            if result["satisfied"]:
                return True

        # Prefer an explicit knowledge-check result when present.
        for result in state.results.values():
            if not isinstance(result, dict):
                continue

            if result.get("satisfied") is True:
                missing_fields = result.get(
                    "missing_fields",
                    [],
                )

                missing_dimensions = result.get(
                    "missing_dimensions",
                    [],
                )

                if not missing_fields and not missing_dimensions:
                    return True

        required_fields = set(requirement.fields)

        if not required_fields:
            return False

        # Candidate discovery returns a top-level `results` list rather than
        # the `data` shape used by single-movie detail retrieval.
        for result in state.results.values():
            if not isinstance(result, dict):
                continue

            candidates = result.get("results")
            if isinstance(candidates, list) and candidates:
                if all(
                    isinstance(candidate, dict)
                    and all(self._value_available(candidate.get(field)) for field in required_fields)
                    for candidate in candidates
                ):
                    return True

        # Fall back to structured task output,
        # e.g. get_movie_details.data.
        for result in state.results.values():
            if not isinstance(result, dict):
                continue

            data = result.get("data")

            if isinstance(data, dict) and all(
                self._value_available(data.get(field)) for field in required_fields
            ):
                return True

        return False

    def _user_context_satisfied(
        self,
        requirement: InformationRequirement,
        state: ExecutionState,
    ) -> bool:
        result = state.results.get("retrieve_user_context")

        if not isinstance(result, dict):
            return False

        if result.get("status") == "available":
            return True

        return result.get("context") is not None

    @staticmethod
    def _value_available(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, (list, dict)) and not value:
            return False
        return True

    @staticmethod
    def _coerce_evidence(values: list[object]) -> list[Evidence]:
        evidence: list[Evidence] = []
        for value in values:
            if isinstance(value, Evidence):
                evidence.append(value)
            elif isinstance(value, dict):
                try:
                    evidence.append(Evidence.model_validate(value))
                except ValidationError as _exc:
                    log_fallback("completeness_fallback", "evidence_validation", _exc)
                    # One malformed/foreign result must not make the whole
                    # execution appear complete.
                    continue
        return evidence
