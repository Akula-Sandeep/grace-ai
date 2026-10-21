import pytest

from app.agent.models import InformationRequirement
from app.knowledge.acquisition import (
    AcquisitionRequest,
    AcquisitionResult,
    EvidenceAcquirer,
    KnowledgeAcquisitionOrchestrator,
)
from app.knowledge.resolver import KnowledgeCheckResult


class FakeAcquirer(EvidenceAcquirer):
    acquisition_type = "web"

    def __init__(self):
        self.requests = []

    async def acquire(
        self,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        self.requests.append(request)

        return AcquisitionResult(
            request=request,
            completed=True,
        )


def structured_requirement():
    return InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )


def qualitative_requirement():
    return InformationRequirement(
        id="visual_darkness",
        type="qualitative",
        subject="Inception",
        dimensions=["visual_darkness"],
        evidence_required=True,
    )


def current_requirement():
    return InformationRequirement(
        id="streaming",
        type="current",
        subject="Inception",
        fields=["streaming_availability"],
        freshness="current",
        evidence_required=True,
    )


def test_satisfied_requirement_does_not_create_request():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    check = KnowledgeCheckResult(
        satisfied=True,
        reason="Already known.",
    )

    request = orchestrator.create_request(
        structured_requirement(),
        check,
        movie_id=27205,
    )

    assert request is None


def test_structured_requirement_creates_structured_request():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_fields=["runtime"],
        reason="Runtime is missing.",
    )

    request = orchestrator.create_request(
        structured_requirement(),
        check,
        movie_id=27205,
    )

    assert request is not None
    assert request.requirement_id == "runtime"
    assert request.movie_id == 27205
    assert request.aspect == "runtime"
    assert request.acquisition_type == "structured"


def test_qualitative_requirement_creates_web_request():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["visual_darkness"],
        reason="Visual darkness is missing.",
    )

    request = orchestrator.create_request(
        qualitative_requirement(),
        check,
        movie_id=27205,
    )

    assert request is not None
    assert request.aspect == "visual_darkness"
    assert request.acquisition_type == "web"


def test_current_requirement_creates_web_request():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_fields=["streaming_availability"],
        reason="Streaming availability is missing.",
    )

    request = orchestrator.create_request(
        current_requirement(),
        check,
        movie_id=27205,
        query="Inception streaming availability",
    )

    assert request is not None
    assert request.aspect == "streaming_availability"
    assert request.acquisition_type == "web"
    assert request.query == ("Inception streaming availability")


@pytest.mark.asyncio
async def test_acquire_invokes_registered_acquirer():
    acquirer = FakeAcquirer()

    orchestrator = KnowledgeAcquisitionOrchestrator(
        acquirers={
            "web": acquirer,
        }
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["visual_darkness"],
        reason="Visual darkness is missing.",
    )

    result = await orchestrator.acquire(
        qualitative_requirement(),
        check,
        movie_id=27205,
    )

    assert result is not None
    assert result.completed is True

    assert len(acquirer.requests) == 1

    request = acquirer.requests[0]

    assert request.movie_id == 27205
    assert request.aspect == "visual_darkness"


@pytest.mark.asyncio
async def test_missing_acquirer_returns_failure_result():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["visual_darkness"],
        reason="Visual darkness is missing.",
    )

    result = await orchestrator.acquire(
        qualitative_requirement(),
        check,
        movie_id=27205,
    )

    assert result is not None
    assert result.completed is False
    assert result.error is not None
    assert "web" in result.error


def test_register_acquirer():
    orchestrator = KnowledgeAcquisitionOrchestrator()
    acquirer = FakeAcquirer()

    orchestrator.register_acquirer(
        "web",
        acquirer,
    )

    assert orchestrator.acquirers["web"] is acquirer


def test_missing_dimension_takes_priority():
    orchestrator = KnowledgeAcquisitionOrchestrator()

    requirement = InformationRequirement(
        id="tone",
        type="qualitative",
        subject="Inception",
        fields=["overview"],
        dimensions=["emotional_tone"],
        evidence_required=True,
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_fields=["overview"],
        missing_dimensions=["emotional_tone"],
        reason="Missing.",
    )

    request = orchestrator.create_request(
        requirement,
        check,
    )

    assert request is not None
    assert request.aspect == "emotional_tone"


def test_model_knowledge_requirement_does_not_create_acquisition_request():
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

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["information_need"],
        reason="Information is not available in the current knowledge.",
    )

    orchestrator = KnowledgeAcquisitionOrchestrator(acquirers={})

    request = orchestrator.create_request(
        requirement,
        check,
        movie_id=27205,
    )

    assert request is None
