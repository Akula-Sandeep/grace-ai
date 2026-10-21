from datetime import UTC, datetime
from typing import Any

from app.agent.models import InformationRequirement

from .evidence import Evidence, EvidenceSet

_CONFIDENCE_RANK = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


class EvidenceResolver:
    """
    Deterministically evaluates whether an EvidenceSet satisfies
    an InformationRequirement.

    This class does not:
    - perform retrieval
    - call an LLM
    - call web search
    - perform RAG
    - perform vision analysis

    It only evaluates already-acquired evidence.
    """

    def check(
        self,
        requirement: InformationRequirement,
        evidence: EvidenceSet | list[Evidence],
    ) -> dict[str, Any]:
        if isinstance(evidence, EvidenceSet):
            evidence_items = evidence.evidence
        else:
            evidence_items = evidence

        required_aspects = self._required_aspects(requirement)

        if not required_aspects:
            return {
                "satisfied": False,
                "missing_aspects": [],
                "matching_evidence": [],
                "reason": ("Requirement does not specify any " "evidence dimensions."),
            }

        matching_evidence = [
            item
            for item in evidence_items
            if self._matches_requirement(
                item,
                requirement,
            )
        ]

        missing_aspects = [
            aspect
            for aspect in required_aspects
            if not self._aspect_satisfied(
                aspect,
                matching_evidence,
                requirement,
            )
        ]

        satisfied = not missing_aspects

        confidence = self._overall_confidence(matching_evidence)

        return {
            "satisfied": satisfied,
            "missing_aspects": missing_aspects,
            "matching_evidence": [item.model_dump() for item in matching_evidence],
            "confidence": confidence,
            "reason": self._reason(
                satisfied=satisfied,
                missing_aspects=missing_aspects,
            ),
        }

    def _required_aspects(
        self,
        requirement: InformationRequirement,
    ) -> list[str]:
        aspects = []

        for aspect in requirement.dimensions:
            if aspect not in aspects:
                aspects.append(aspect)

        for field in requirement.fields:
            if field not in aspects:
                aspects.append(field)

        return aspects

    def _matches_requirement(
        self,
        evidence: Evidence,
        requirement: InformationRequirement,
    ) -> bool:
        required_aspects = self._required_aspects(requirement)

        if evidence.aspect not in required_aspects:
            return False

        if not self._confidence_satisfied(
            evidence,
            requirement,
        ):
            return False

        if not self._freshness_satisfied(
            evidence,
            requirement,
        ):
            return False

        return True

    def _aspect_satisfied(
        self,
        aspect: str,
        evidence: list[Evidence],
        requirement: InformationRequirement,
    ) -> bool:
        matching = [item for item in evidence if item.aspect == aspect]

        if not matching:
            return False

        return any(
            self._confidence_satisfied(
                item,
                requirement,
            )
            and self._freshness_satisfied(
                item,
                requirement,
            )
            and self._value_satisfies_requirement(
                item,
                requirement,
            )
            for item in matching
        )

    @staticmethod
    def _value_satisfies_requirement(
        evidence: Evidence,
        requirement: InformationRequirement,
    ) -> bool:
        desired = requirement.desired_value

        # No semantic value requested.
        # Aspect + confidence + freshness are sufficient.
        if desired is None:
            return True

        # We cannot prove a semantic requirement from evidence
        # that has no normalized value.
        if evidence.value is None:
            return False

        polarity = requirement.polarity or "prefer"

        values_match = evidence.value == desired

        if polarity in {"prefer", "require"}:
            return values_match

        if polarity == "avoid":
            return not values_match

        return False

    def _confidence_satisfied(
        self,
        evidence: Evidence,
        requirement: InformationRequirement,
    ) -> bool:
        return _CONFIDENCE_RANK[evidence.confidence] >= _CONFIDENCE_RANK[requirement.minimum_confidence]

    def _freshness_satisfied(
        self,
        evidence: Evidence,
        requirement: InformationRequirement,
    ) -> bool:
        if requirement.freshness == "stable":
            return True

        if evidence.published_at is None:
            return False

        now = datetime.now(UTC)

        published_at = evidence.published_at

        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)

        age_days = (now - published_at).total_seconds() / 86400
        if age_days < 0:
            return False

        if requirement.freshness == "recent":
            return age_days <= 30

        if requirement.freshness == "current":
            return age_days <= 7

        return False

    def _overall_confidence(
        self,
        evidence: list[Evidence],
    ) -> str:
        if not evidence:
            return "low"

        highest_rank = max(_CONFIDENCE_RANK[item.confidence] for item in evidence)

        for name, rank in _CONFIDENCE_RANK.items():
            if rank == highest_rank:
                return name

        return "low"

    def _reason(
        self,
        *,
        satisfied: bool,
        missing_aspects: list[str],
    ) -> str:
        if satisfied:
            return "Sufficient matching evidence is available " "for all required aspects."

        if missing_aspects:
            return "Evidence is insufficient for required " f"aspects: {', '.join(missing_aspects)}."

        return "Evidence requirement is not satisfied."
