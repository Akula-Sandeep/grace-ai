from datetime import UTC, datetime, timedelta

from app.agent.models import InformationRequirement
from app.agent.tools import MovieTools
from app.knowledge.evidence import Evidence
from app.knowledge.evidence_resolver import EvidenceResolver


def test_movie_search_prioritizes_exact_title_and_requested_year():
    results = MovieTools._rank_movie_candidates(
        [
            {
                "id": 1,
                "title": "Example Film",
                "release_date": "2019-01-01",
                "popularity": 50,
            },
            {
                "id": 2,
                "title": "Example Film",
                "release_date": "2021-01-01",
                "popularity": 5,
            },
            {
                "id": 3,
                "title": "Example Film: Returns",
                "release_date": "2021-01-01",
                "popularity": 100,
            },
        ],
        "Example Film",
        requested_year=2021,
    )

    assert [item["id"] for item in results] == [2, 3, 1]


def test_future_published_evidence_does_not_satisfy_freshness():
    requirement = InformationRequirement(
        id="freshness-check",
        type="qualitative",
        subject="movie",
        dimensions=["reception"],
        freshness="current",
        evidence_required=True,
    )
    evidence = Evidence(
        id="future",
        movie_id=1,
        source_type="web",
        source_name="Test",
        aspect="reception",
        content="Future-dated claim",
        confidence="high",
        published_at=datetime.now(UTC) + timedelta(days=2),
    )

    result = EvidenceResolver().check(requirement, [evidence])

    assert result["satisfied"] is False
    assert result["missing_aspects"] == ["reception"]
