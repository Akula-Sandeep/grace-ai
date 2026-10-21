# knowledge/acquisition/orchestrator.py
from collections.abc import Mapping

from app.agent.models import InformationRequirement
from app.knowledge.resolver import (
    KnowledgeCheckResult,
    KnowledgeResolver,
)
from app.observability import logger

from .base import EvidenceAcquirer
from .models import (
    AcquisitionRequest,
    AcquisitionResult,
)
from .router import KnowledgeAcquisitionRouter


class KnowledgeAcquisitionOrchestrator:
    """
    Coordinates the acquisition of missing knowledge.

    Responsibilities:
    - inspect an information requirement
    - use the knowledge check to determine what is missing
    - route the acquisition request
    - invoke the selected acquirer
    - return the acquired evidence

    It does not know how any individual source works.
    """

    def __init__(
        self,
        *,
        router: KnowledgeAcquisitionRouter | None = None,
        resolver: KnowledgeResolver | None = None,
        acquirers: Mapping[str, EvidenceAcquirer] | None = None,
    ) -> None:
        self.resolver = resolver or KnowledgeResolver()
        self.acquirers = dict(acquirers or {})
        available_sources = {
            {
                "structured": "tmdb",
                "web": "web",
                "rag": "rag",
                "vision": "vision",
            }.get(acquisition_type, acquisition_type)
            for acquisition_type in self.acquirers
        }
        if "web" in self.acquirers:
            available_sources.add("qualitative_evidence")
        self.router = router or KnowledgeAcquisitionRouter(available_sources=available_sources or None)
        logger.debug(
            "acquisition_orchestrator_init acquirers=%s available_sources=%s",
            list(self.acquirers.keys()),
            self.router.available_sources,
        )

    def register_acquirer(
        self,
        source: str,
        acquirer: EvidenceAcquirer,
    ) -> None:
        self.acquirers[source] = acquirer

    def create_request(
        self,
        requirement: InformationRequirement,
        check: KnowledgeCheckResult,
        *,
        movie_id: int | None = None,
        query: str | None = None,
    ) -> AcquisitionRequest | None:
        if not requirement.acquisition_required:
            return None
        decision = self.router.decide(
            requirement,
            check,
        )

        if decision.source == "none":
            return None

        acquisition_type = self._map_source(decision.source)

        missing_aspect = self._get_aspect(
            requirement,
            check,
        )

        return AcquisitionRequest(
            requirement_id=requirement.id,
            movie_id=movie_id,
            aspect=missing_aspect,
            acquisition_type=acquisition_type,
            query=query,
            desired_value=requirement.desired_value,
            polarity=requirement.polarity,
            importance=requirement.importance,
        )

    async def acquire(
        self,
        requirement: InformationRequirement,
        check: KnowledgeCheckResult,
        *,
        movie_id: int | None = None,
        query: str | None = None,
    ) -> AcquisitionResult | None:
        request = self.create_request(
            requirement,
            check,
            movie_id=movie_id,
            query=query,
        )

        if request is None:
            return None

        decision = self.router.decide(
            requirement,
            check,
        )

        acquisition_type = self._map_source(decision.source)

        acquirer = self.acquirers.get(acquisition_type)

        if acquirer is None:
            return AcquisitionResult(
                request=request,
                completed=False,
                error=(f"No acquirer registered for " f"acquisition type: {acquisition_type}"),
            )

        return await acquirer.acquire(request)

    @staticmethod
    def _map_source(
        source: str,
    ) -> str:
        mapping = {
            "tmdb": "structured",
            "qualitative_evidence": "web",
            "web": "web",
            "rag": "rag",
            "vision": "vision",
        }

        return mapping.get(
            source,
            "web",
        )

    @staticmethod
    def _get_aspect(
        requirement: InformationRequirement,
        check: KnowledgeCheckResult,
    ) -> str:
        if check.missing_dimensions:
            return check.missing_dimensions[0]

        if check.missing_fields:
            return check.missing_fields[0]

        if requirement.dimensions:
            return requirement.dimensions[0]

        if requirement.fields:
            return requirement.fields[0]

        return requirement.id
