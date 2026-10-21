# file: Backend/app/knowledge/resolver.py
from pydantic import BaseModel, Field

from app.agent.models import InformationRequirement
from app.knowledge.models import MovieKnowledge


class KnowledgeCheckResult(BaseModel):
    satisfied: bool

    missing_fields: list[str] = Field(default_factory=list)

    missing_dimensions: list[str] = Field(default_factory=list)

    reason: str


class KnowledgeResolver:
    """
    Determines whether existing movie knowledge is sufficient
    to satisfy an information requirement.

    This class performs no network calls and no LLM inference.
    """

    def check(
        self,
        requirement: InformationRequirement,
        knowledge: MovieKnowledge | None,
    ) -> KnowledgeCheckResult:
        if knowledge is None:
            return KnowledgeCheckResult(
                satisfied=False,
                missing_fields=requirement.fields.copy(),
                missing_dimensions=requirement.dimensions.copy(),
                reason="No knowledge exists for the requested subject.",
            )

        missing_fields = [
            field
            for field in requirement.fields
            if not self._field_available(
                knowledge,
                field,
            )
        ]

        missing_dimensions = [
            dimension
            for dimension in requirement.dimensions
            if not self._dimension_available(
                knowledge,
                dimension,
            )
        ]

        satisfied = not missing_fields and not missing_dimensions

        if satisfied:
            reason = "Existing knowledge satisfies the requirement."
        else:
            reason = "Required information is missing."

        return KnowledgeCheckResult(
            satisfied=satisfied,
            missing_fields=missing_fields,
            missing_dimensions=missing_dimensions,
            reason=reason,
        )

    @staticmethod
    def _field_available(
        knowledge: MovieKnowledge,
        field: str,
    ) -> bool:
        value = getattr(
            knowledge,
            field,
            None,
        )

        if value is None:
            return False

        if isinstance(value, str) and not value.strip():
            return False

        if isinstance(value, (list, dict)) and not value:
            return False

        return True

    @staticmethod
    def _dimension_available(
        knowledge: MovieKnowledge,
        dimension: str,
    ) -> bool:
        """
        Qualitative dimensions are intentionally not treated as
        ordinary MovieKnowledge fields yet.

        They will eventually be represented by a dedicated
        evidence/qualitative knowledge model.
        """

        return False
