from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.knowledge.evidence import (
    Evidence,
    EvidenceSet,
)


def test_structured_evidence_is_valid():
    evidence = Evidence(
        id="e1",
        movie_id=123,
        source_type="structured",
        source_name="TMDB",
        aspect="runtime",
        content="The movie has a runtime of 148 minutes.",
        confidence="high",
    )

    assert evidence.movie_id == 123
    assert evidence.source_type == "structured"
    assert evidence.aspect == "runtime"
    assert evidence.confidence == "high"
    assert evidence.spoiler_level == "none"
    assert evidence.retrieved_at.tzinfo is not None


def test_web_evidence_is_valid():
    published_at = datetime(
        2026,
        8,
        20,
        tzinfo=UTC,
    )

    evidence = Evidence(
        id="e2",
        movie_id=123,
        source_type="web",
        source_name="Example Review",
        source_url="https://example.com/review",
        aspect="atmosphere",
        content="The film has a slow, oppressive atmosphere.",
        confidence="medium",
        published_at=published_at,
    )

    assert evidence.source_type == "web"
    assert evidence.source_url == "https://example.com/review"
    assert evidence.published_at == published_at


def test_visual_evidence_is_valid():
    evidence = Evidence(
        id="e3",
        movie_id=123,
        source_type="vision",
        source_name="movie_visual_analysis",
        aspect="visual_darkness",
        content="The sampled frames predominantly use low-key lighting.",
        confidence="medium",
    )

    assert evidence.source_type == "vision"
    assert evidence.aspect == "visual_darkness"


def test_invalid_source_type_is_rejected():
    with pytest.raises(ValidationError):
        Evidence(
            id="e4",
            movie_id=123,
            source_type="unknown",
            source_name="test",
            aspect="runtime",
            content="test",
        )


def test_invalid_confidence_is_rejected():
    with pytest.raises(ValidationError):
        Evidence(
            id="e5",
            movie_id=123,
            source_type="web",
            source_name="test",
            aspect="atmosphere",
            content="test",
            confidence="very_high",
        )


def test_invalid_spoiler_level_is_rejected():
    with pytest.raises(ValidationError):
        Evidence(
            id="e6",
            movie_id=123,
            source_type="web",
            source_name="test",
            aspect="plot",
            content="test",
            spoiler_level="extreme",
        )


def test_evidence_set_can_contain_multiple_evidence_items():
    evidence_set = EvidenceSet(
        requirement_id="req_atmosphere",
        evidence=[
            Evidence(
                id="e7",
                movie_id=123,
                source_type="critic",
                source_name="Critic Review",
                aspect="atmosphere",
                content="The atmosphere is tense and oppressive.",
                confidence="high",
            ),
            Evidence(
                id="e8",
                movie_id=123,
                source_type="vision",
                source_name="movie_visual_analysis",
                aspect="visual_darkness",
                content="The sampled frames use predominantly low-key lighting.",
                confidence="medium",
            ),
        ],
    )

    assert len(evidence_set.evidence) == 2
    assert evidence_set.sufficient is False
    assert evidence_set.confidence == "low"


def test_insufficient_evidence_records_missing_aspects():
    evidence_set = EvidenceSet(
        requirement_id="req_visual_style",
        evidence=[],
        sufficient=False,
        confidence="low",
        missing_aspects=[
            "visual_darkness",
            "cinematography",
        ],
    )

    assert evidence_set.sufficient is False
    assert evidence_set.confidence == "low"
    assert evidence_set.missing_aspects == [
        "visual_darkness",
        "cinematography",
    ]


def test_sufficient_evidence_set():
    evidence_set = EvidenceSet(
        requirement_id="req_runtime",
        evidence=[
            Evidence(
                id="e9",
                movie_id=123,
                source_type="structured",
                source_name="TMDB",
                aspect="runtime",
                content="148 minutes",
                confidence="high",
            )
        ],
        sufficient=True,
        confidence="high",
    )

    assert evidence_set.sufficient is True
    assert evidence_set.confidence == "high"
    assert evidence_set.missing_aspects == []
