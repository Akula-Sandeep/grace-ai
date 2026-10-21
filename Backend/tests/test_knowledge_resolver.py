import time

from app.agent.models import InformationRequirement
from app.knowledge.models import MovieKnowledge
from app.knowledge.resolver import KnowledgeResolver


def test_runtime_requirement_is_satisfied():
    resolver = KnowledgeResolver()

    knowledge = MovieKnowledge(
        movie_id=27205,
        title="Inception",
        runtime=148,
    )

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    result = resolver.check(
        requirement,
        knowledge,
    )

    assert result.satisfied is True
    assert result.missing_fields == []


def test_missing_runtime_is_detected():
    resolver = KnowledgeResolver()

    knowledge = MovieKnowledge(
        movie_id=27205,
        title="Inception",
    )

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    result = resolver.check(
        requirement,
        knowledge,
    )

    assert result.satisfied is False
    assert result.missing_fields == ["runtime"]


def test_missing_movie_is_detected():
    resolver = KnowledgeResolver()

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    result = resolver.check(
        requirement,
        None,
    )

    assert result.satisfied is False
    assert result.missing_fields == ["runtime"]


def test_qualitative_information_is_not_falsely_claimed():
    resolver = KnowledgeResolver()

    knowledge = MovieKnowledge(
        movie_id=27205,
        title="Inception",
        runtime=148,
    )

    requirement = InformationRequirement(
        id="visual_darkness",
        type="qualitative",
        subject="Inception",
        dimensions=["visual_darkness"],
        evidence_required=True,
    )

    result = resolver.check(
        requirement,
        knowledge,
    )

    assert result.satisfied is False
    assert result.missing_dimensions == ["visual_darkness"]


def test_knowledge_check_is_very_fast():
    resolver = KnowledgeResolver()

    knowledge = MovieKnowledge(
        movie_id=27205,
        title="Inception",
        runtime=148,
    )

    requirement = InformationRequirement(
        id="runtime",
        type="structured",
        subject="Inception",
        fields=["runtime"],
    )

    started = time.perf_counter()

    for _ in range(10_000):
        result = resolver.check(
            requirement,
            knowledge,
        )

    elapsed = time.perf_counter() - started

    assert result.satisfied is True

    # This should be vastly cheaper than network retrieval.
    assert elapsed < 0.5
