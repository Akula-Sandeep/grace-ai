from datetime import UTC, datetime, timedelta

from app.agent.models import InformationRequirement
from app.knowledge.evidence import Evidence, EvidenceSet
from app.knowledge.evidence_resolver import EvidenceResolver


def make_requirement(
    *,
    dimensions=None,
    minimum_confidence="medium",
    freshness="stable",
):
    return InformationRequirement(
        id="req-1",
        type="qualitative",
        subject="movie",
        dimensions=dimensions or [],
        minimum_confidence=minimum_confidence,
        freshness=freshness,
        evidence_required=True,
    )


def make_evidence(
    *,
    aspect="visual_darkness",
    confidence="high",
    published_at=None,
):
    return Evidence(
        id="e1",
        movie_id=123,
        source_type="critic",
        source_name="Test Source",
        aspect=aspect,
        content="Test evidence.",
        confidence=confidence,
        published_at=published_at,
    )


def test_matching_high_confidence_evidence_satisfies_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(dimensions=["visual_darkness"])

    evidence = EvidenceSet(
        requirement_id="req-1",
        evidence=[
            make_evidence(
                aspect="visual_darkness",
                confidence="high",
            )
        ],
    )

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is True
    assert result["missing_aspects"] == []
    assert result["confidence"] == "high"


def test_missing_aspect_is_not_satisfied():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=[
            "visual_darkness",
            "cinematography",
        ]
    )

    evidence = EvidenceSet(
        requirement_id="req-1",
        evidence=[make_evidence(aspect="visual_darkness")],
    )

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["cinematography"]


def test_wrong_aspect_does_not_satisfy_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(dimensions=["visual_darkness"])

    evidence = EvidenceSet(
        requirement_id="req-1",
        evidence=[make_evidence(aspect="atmosphere")],
    )

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["visual_darkness"]


def test_insufficient_confidence_is_rejected():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["visual_darkness"],
        minimum_confidence="high",
    )

    evidence = EvidenceSet(
        requirement_id="req-1",
        evidence=[
            make_evidence(
                aspect="visual_darkness",
                confidence="medium",
            )
        ],
    )

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["visual_darkness"]


def test_high_confidence_satisfies_medium_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["visual_darkness"],
        minimum_confidence="medium",
    )

    evidence = [
        make_evidence(
            aspect="visual_darkness",
            confidence="high",
        )
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is True


def test_matching_semantic_value_satisfies_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["visual_darkness"],
    )
    requirement.desired_value = "dark"
    requirement.polarity = "prefer"

    evidence = [
        make_evidence(
            aspect="visual_darkness",
            confidence="high",
        ).model_copy(update={"value": "dark"})
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is True


def test_conflicting_semantic_value_does_not_satisfy_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["visual_darkness"],
    )
    requirement.desired_value = "dark"
    requirement.polarity = "require"

    evidence = [
        make_evidence(
            aspect="visual_darkness",
            confidence="high",
        ).model_copy(update={"value": "bright"})
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["visual_darkness"]


def test_recent_requirement_rejects_old_evidence():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["reception"],
        freshness="recent",
    )

    old_date = datetime.now(UTC) - timedelta(days=60)

    evidence = [
        make_evidence(
            aspect="reception",
            published_at=old_date,
        )
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["reception"]


def test_recent_requirement_accepts_recent_evidence():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["reception"],
        freshness="recent",
    )

    recent_date = datetime.now(UTC) - timedelta(days=5)

    evidence = [
        make_evidence(
            aspect="reception",
            published_at=recent_date,
        )
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is True


def test_current_requirement_accepts_recent_evidence():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["current_reception"],
        freshness="current",
    )

    recent_date = datetime.now(UTC) - timedelta(days=2)

    evidence = [
        make_evidence(
            aspect="current_reception",
            published_at=recent_date,
        )
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is True


def test_current_requirement_rejects_old_evidence():
    resolver = EvidenceResolver()

    requirement = make_requirement(
        dimensions=["current_reception"],
        freshness="current",
    )

    old_date = datetime.now(UTC) - timedelta(days=20)

    evidence = [
        make_evidence(
            aspect="current_reception",
            published_at=old_date,
        )
    ]

    result = resolver.check(
        requirement,
        evidence,
    )

    assert result["satisfied"] is False


def test_empty_evidence_does_not_satisfy_requirement():
    resolver = EvidenceResolver()

    requirement = make_requirement(dimensions=["atmosphere"])

    result = resolver.check(
        requirement,
        EvidenceSet(requirement_id="req-1"),
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["atmosphere"]


def test_requirement_without_aspects_is_not_satisfied():
    resolver = EvidenceResolver()

    requirement = make_requirement()

    result = resolver.check(
        requirement,
        [],
    )

    assert result["satisfied"] is False
    assert result["missing_aspects"] == []
