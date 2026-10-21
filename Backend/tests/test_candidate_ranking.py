# test_candidate_ranking.py

from app.agent.candidate_ranking import CandidateRanker
from app.agent.models import InformationRequirement
from app.knowledge.evidence import Evidence


def req(id_, dimension, desired, importance=1.0):
    return InformationRequirement(
        id=id_,
        type="qualitative",
        subject="candidate movies",
        dimensions=[dimension],
        desired_value=desired,
        importance=importance,
    )


def ev(id_, movie_id, aspect, value, confidence="high"):
    return Evidence(
        id=id_,
        movie_id=movie_id,
        source_type="web",
        source_name="test",
        aspect=aspect,
        value=value,
        content="test evidence",
        confidence=confidence,
    )


def test_ranker_prefers_candidate_matching_semantic_requirement():
    ranker = CandidateRanker()
    result = ranker.rank(
        candidates=[
            {"id": 1, "title": "Bright Movie", "vote_average": 9.0},
            {"id": 2, "title": "Dark Movie", "vote_average": 7.0},
        ],
        requirements=[req("visual", "visual_darkness", "dark")],
        evidence=[
            ev("e1", 1, "visual_darkness", "bright"),
            ev("e2", 2, "visual_darkness", "dark"),
        ],
    )

    assert result["candidates"][0]["id"] == 2
    assert result["candidates"][0]["rank_score"] == 1.0
    assert result["candidates"][0]["matched_requirements"] == ["visual"]


def test_ranker_keeps_tmdb_order_when_no_semantic_requirements():
    ranker = CandidateRanker()
    result = ranker.rank(
        candidates=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        requirements=[],
        evidence=[],
        count=1,
    )

    assert result["candidates"][0]["id"] == 1
    assert result["ranking_basis"] == "tmdb_candidate_order"


def test_ranker_preserves_unknown_candidates_but_marks_missing_evidence():
    ranker = CandidateRanker()
    result = ranker.rank(
        candidates=[{"id": 1, "title": "Unknown"}],
        requirements=[req("visual", "visual_darkness", "dark")],
        evidence=[],
    )

    candidate = result["candidates"][0]
    assert candidate["rank_score"] == 0.0
    assert candidate["evidence_coverage"] == 0.0
    assert candidate["unmatched_requirements"] == ["visual"]


def test_ranker_uses_neutral_commercial_score_for_missing_metrics():
    ranker = CandidateRanker()
    result = ranker.rank(
        candidates=[
            {"id": 1, "title": "Indie", "vote_average": 7.0},
            {"id": 2, "title": "International"},
        ],
        requirements=[],
        evidence=[],
    )

    assert result["candidates"][1]["commercial_score"] == 0.5


def test_ranker_compares_relative_semantic_intensity_to_reference():
    ranker = CandidateRanker()
    requirement = InformationRequirement(
        id="visual",
        type="qualitative",
        subject="candidate movies",
        dimensions=["visual_darkness"],
        desired_value="dark",
        comparison="greater_than_reference",
        importance=1.0,
    )

    result = ranker.rank(
        candidates=[
            {"id": 10, "title": "Reference"},
            {"id": 20, "title": "Darker"},
            {"id": 30, "title": "Lighter"},
        ],
        requirements=[requirement],
        evidence=[
            ev("ref", 10, "visual_darkness", 0.50),
            ev("dark", 20, "visual_darkness", 0.85),
            ev("light", 30, "visual_darkness", 0.35),
        ],
        reference_movie_id=10,
        count=2,
    )

    assert [item["id"] for item in result["candidates"]] == [20, 30]
    assert result["candidates"][0]["rank_score"] == 1.0
    assert result["candidates"][1]["rank_score"] == 0.0


def test_ranker_does_not_make_relative_claim_without_reference_evidence():
    ranker = CandidateRanker()
    requirement = InformationRequirement(
        id="visual",
        type="qualitative",
        subject="candidate movies",
        dimensions=["visual_darkness"],
        desired_value="dark",
        comparison="greater_than_reference",
        importance=1.0,
    )

    result = ranker.rank(
        candidates=[{"id": 20, "title": "Candidate"}],
        requirements=[requirement],
        evidence=[ev("candidate", 20, "visual_darkness", 0.9)],
        reference_movie_id=10,
    )

    assert result["candidates"][0]["rank_score"] == 0.0
    assert result["candidates"][0]["evidence_coverage"] == 0.0


def test_ranker_reports_requirement_evaluation_for_completeness():
    ranker = CandidateRanker()
    requirement = req("visual", "visual_darkness", "dark")

    result = ranker.rank(
        candidates=[{"id": 1, "title": "Dark"}],
        requirements=[requirement],
        evidence=[ev("e1", 1, "visual_darkness", "dark")],
        count=1,
    )

    assert result["evaluated_requirements"] == ["visual"]
    assert result["requirement_evaluation"]["visual"]["covered_candidate_count"] == 1


def test_completeness_uses_candidate_ranking_evaluation():
    from app.agent.completeness import ExecutionCompleteness
    from app.agent.models import ExecutionPlan, ExecutionPolicy, ExecutionState

    requirement = req("visual", "visual_darkness", "dark")
    plan = ExecutionPlan(
        policy=ExecutionPolicy(path="targeted", reasoning_level="medium"),
        information_requirements=[requirement],
    )
    state = ExecutionState(plan=plan)
    state.results["rank_candidates"] = {
        "evaluated_requirements": ["visual"],
        "requirement_evaluation": {
            "visual": {
                "evaluated": True,
                "covered_candidate_count": 1,
            }
        },
        "candidates": [
            {
                "id": 1,
                "title": "Dark",
                "evidence_coverage": 1.0,
            }
        ],
    }

    complete, unresolved = ExecutionCompleteness().evaluate(plan, state)

    assert complete is True
    assert unresolved == []


def test_completeness_does_not_accept_empty_ranking_evaluation():
    from app.agent.completeness import ExecutionCompleteness
    from app.agent.models import (
        ExecutionPlan,
        ExecutionPolicy,
        ExecutionState,
    )

    requirement = req(
        "visual",
        "visual_darkness",
        "dark",
    )

    plan = ExecutionPlan(
        policy=ExecutionPolicy(
            path="targeted",
            reasoning_level="medium",
        ),
        information_requirements=[requirement],
    )

    state = ExecutionState(plan=plan)

    state.results["rank_candidates"] = {
        "evaluated_requirements": ["visual"],
        "requirement_evaluation": {
            "visual": {
                "evaluated": True,
                "covered_candidate_count": 0,
            }
        },
        "candidates": [],
    }

    complete, unresolved = ExecutionCompleteness().evaluate(plan, state)

    assert complete is False
    assert unresolved == ["visual"]


def test_ranker_does_not_mark_requirement_covered_by_other_requirement_evidence():
    ranker = CandidateRanker()

    visual = req(
        "visual",
        "visual_darkness",
        "dark",
    )
    emotional = req(
        "emotional",
        "emotional_heaviness",
        "heavy",
    )

    result = ranker.rank(
        candidates=[
            {"id": 1, "title": "Movie"},
        ],
        requirements=[visual, emotional],
        evidence=[
            ev(
                "visual-evidence",
                1,
                "visual_darkness",
                "dark",
            ),
        ],
    )

    assert result["requirement_evaluation"]["visual"] == {
        "evaluated": True,
        "covered_candidate_count": 1,
    }

    assert result["requirement_evaluation"]["emotional"] == {
        "evaluated": False,
        "covered_candidate_count": 0,
    }
