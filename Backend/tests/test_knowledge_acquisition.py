from app.agent.models import InformationRequirement
from app.knowledge.acquisition import (
    KnowledgeAcquisitionRouter,
)
from app.knowledge.resolver import (
    KnowledgeCheckResult,
)


def test_runtime_uses_tmdb():
    router = KnowledgeAcquisitionRouter()

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_fields=["runtime"],
        reason="Runtime is missing.",
    )

    decision = router.decide(
        requirement,
        check,
    )

    assert decision.source == "tmdb"
    assert decision.missing_fields == ["runtime"]


def test_visual_darkness_uses_qualitative_evidence():
    router = KnowledgeAcquisitionRouter()

    requirement = InformationRequirement(
        id="visual_darkness",
        type="qualitative",
        subject="Inception",
        dimensions=["visual_darkness"],
        evidence_required=True,
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["visual_darkness"],
        reason="Visual darkness is missing.",
    )

    decision = router.decide(
        requirement,
        check,
    )

    assert decision.source == "qualitative_evidence"
    assert decision.missing_dimensions == ["visual_darkness"]


def test_current_information_uses_web():
    router = KnowledgeAcquisitionRouter()

    requirement = InformationRequirement(
        id="streaming",
        type="current",
        subject="Inception",
        fields=["streaming_availability"],
        freshness="current",
        evidence_required=True,
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        missing_fields=["streaming_availability"],
        reason="Streaming availability is missing.",
    )

    decision = router.decide(
        requirement,
        check,
    )

    assert decision.source == "web"


def test_satisfied_requirement_needs_no_source():
    router = KnowledgeAcquisitionRouter()

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    check = KnowledgeCheckResult(
        satisfied=True,
        reason="Already known.",
    )

    decision = router.decide(
        requirement,
        check,
    )

    assert decision.source == "none"


def test_unknown_requirement_does_not_guess_source():
    router = KnowledgeAcquisitionRouter()

    requirement = InformationRequirement(
        id="unknown",
        type="user_context",
        subject="Inception",
    )

    check = KnowledgeCheckResult(
        satisfied=False,
        reason="Information missing.",
    )

    decision = router.decide(
        requirement,
        check,
    )

    assert decision.source == "none"


def test_visual_qualitative_prefers_vision_when_available():
    router = KnowledgeAcquisitionRouter(available_sources={"web", "vision"})
    requirement = InformationRequirement(
        id="visual_darkness",
        type="qualitative",
        subject="Inception",
        dimensions=["visual_darkness"],
        evidence_required=True,
    )
    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["visual_darkness"],
        reason="Visual darkness is missing.",
    )

    decision = router.decide(requirement, check)

    assert decision.source == "vision"
    assert decision.alternatives == ["qualitative_evidence"]


def test_stable_qualitative_prefers_rag_when_available():
    router = KnowledgeAcquisitionRouter(available_sources={"web", "rag"})
    requirement = InformationRequirement(
        id="tone",
        type="qualitative",
        subject="Inception",
        dimensions=["emotional_tone"],
        evidence_required=True,
    )
    check = KnowledgeCheckResult(
        satisfied=False,
        missing_dimensions=["emotional_tone"],
        reason="Tone is missing.",
    )

    decision = router.decide(requirement, check)

    assert decision.source == "rag"
    assert decision.alternatives == ["qualitative_evidence"]
