# app/knowledge/acquisition/router.py
from app.agent.models import InformationRequirement
from app.knowledge.resolver import KnowledgeCheckResult


class KnowledgeAcquisitionDecision:
    """Result of deciding how missing knowledge should be acquired."""

    def __init__(
        self,
        source: str,
        *,
        missing_fields: list[str] | None = None,
        missing_dimensions: list[str] | None = None,
        alternatives: list[str] | None = None,
        reason: str = "",
    ) -> None:
        self.source = source
        self.missing_fields = missing_fields or []
        self.missing_dimensions = missing_dimensions or []
        self.alternatives = alternatives or []
        self.reason = reason


class KnowledgeAcquisitionRouter:
    """
    Determines the best available acquisition mechanism for an
    information need.

    Routing is based on the *kind of evidence needed*, not merely the
    requirement's broad type. Availability is considered so that future
    mechanisms (RAG, vision, etc.) can be plugged in without changing the
    routing contract.
    """

    def __init__(self, available_sources: set[str] | None = None) -> None:
        # The current system has structured + web acquisition. Future
        # mechanisms can become preferred simply by being registered.
        self.available_sources = set(available_sources or {"tmdb", "web", "qualitative_evidence"})

    def decide(
        self,
        requirement: InformationRequirement,
        check: KnowledgeCheckResult,
    ) -> KnowledgeAcquisitionDecision:
        if check.satisfied:
            return KnowledgeAcquisitionDecision(
                source="none", reason="Knowledge already satisfies the requirement."
            )

        missing_fields = list(check.missing_fields)
        missing_dimensions = list(check.missing_dimensions)

        if requirement.type == "structured" and missing_fields:
            return self._decision(
                "tmdb",
                missing_fields=missing_fields,
                reason="Structured movie fields are best acquired from TMDB.",
            )

        if requirement.type == "current":
            return self._decision(
                "web",
                missing_fields=missing_fields,
                missing_dimensions=missing_dimensions,
                reason="Current information requires fresh external evidence.",
            )

        if requirement.type == "research":
            return self._decision(
                "web",
                missing_fields=missing_fields,
                missing_dimensions=missing_dimensions,
                reason="Research requires broad external evidence.",
            )

        if requirement.type == "qualitative":
            return self._qualitative_decision(
                requirement,
                missing_fields,
                missing_dimensions,
            )

        return KnowledgeAcquisitionDecision(
            source="none",
            missing_fields=missing_fields,
            missing_dimensions=missing_dimensions,
            reason="No safe acquisition mechanism is available for this requirement.",
        )

    def _qualitative_decision(
        self,
        requirement: InformationRequirement,
        missing_fields: list[str],
        missing_dimensions: list[str],
    ) -> KnowledgeAcquisitionDecision:
        dimensions = {d.lower() for d in missing_dimensions or requirement.dimensions}

        # Visual questions are unusually well suited to visual evidence, but
        # only use it when an actual vision acquirer is available. Otherwise
        # fall back to web evidence rather than pretending vision exists.
        if dimensions & {
            "visual_style",
            "visual_darkness",
            "cinematography",
            "color_palette",
            "lighting",
        }:
            preferred = ["vision", "qualitative_evidence"]
        else:
            # Stable semantic movie knowledge is a natural future RAG target.
            # Until RAG is actually registered, web remains the safe fallback.
            preferred = ["rag", "qualitative_evidence"]

        return self._first_available(
            preferred,
            missing_fields=missing_fields,
            missing_dimensions=missing_dimensions,
            reason="Qualitative dimensions require semantic evidence; source preference depends on the dimension and available evidence mechanisms.",
        )

    def _first_available(
        self,
        preferred: list[str],
        *,
        missing_fields: list[str],
        missing_dimensions: list[str],
        reason: str,
    ) -> KnowledgeAcquisitionDecision:
        source = next(
            (candidate for candidate in preferred if candidate in self.available_sources),
            None,
        )
        if source is None:
            return KnowledgeAcquisitionDecision(
                source="none",
                missing_fields=missing_fields,
                missing_dimensions=missing_dimensions,
                alternatives=preferred,
                reason="No preferred acquisition mechanism is currently available.",
            )

        alternatives = [candidate for candidate in preferred if candidate != source]
        return KnowledgeAcquisitionDecision(
            source=source,
            missing_fields=missing_fields,
            missing_dimensions=missing_dimensions,
            alternatives=alternatives,
            reason=reason,
        )

    def _decision(
        self,
        source: str,
        *,
        missing_fields: list[str] | None = None,
        missing_dimensions: list[str] | None = None,
        reason: str,
    ) -> KnowledgeAcquisitionDecision:
        if source not in self.available_sources:
            return KnowledgeAcquisitionDecision(
                source="none",
                missing_fields=missing_fields,
                missing_dimensions=missing_dimensions,
                alternatives=[source],
                reason=f"Required source '{source}' is not currently available.",
            )
        return KnowledgeAcquisitionDecision(
            source=source,
            missing_fields=missing_fields,
            missing_dimensions=missing_dimensions,
            reason=reason,
        )
