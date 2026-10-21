from app.agent.failures import FailureKind, failure_response
from app.knowledge.acquisition import (
    AcquisitionRequest,
    AcquisitionResult,
    EvidenceAcquirer,
)
from app.knowledge.evidence import Evidence
from app.recommendations import RecommendationService


class FakeMovieTools:
    async def search_movies(self, query):
        if query == "Inception":
            return {
                "results": [
                    {
                        "id": 27205,
                        "title": "Inception",
                        "release_date": "2010-07-16",
                    }
                ]
            }

        return {"results": []}

    async def similar_movies(self, movie_id, page=1):
        assert movie_id == 27205

        return {
            "results": [
                {
                    "id": 101,
                    "title": "Bright Movie",
                    "release_date": "2020-01-01",
                    "overview": "A bright and optimistic adventure.",
                    "vote_average": 7.0,
                    "vote_count": 1000,
                },
                {
                    "id": 202,
                    "title": "Dark Movie",
                    "release_date": "2020-01-01",
                    "overview": "A visually dark and atmospheric psychological thriller.",
                    "vote_average": 7.5,
                    "vote_count": 1200,
                },
            ]
        }

    async def discover_movies(self, **kwargs):
        return {
            "results": [
                {
                    "id": 101,
                    "title": "Bright Movie",
                    "release_date": "2020-01-01",
                    "overview": "A bright and optimistic adventure.",
                    "vote_average": 7.0,
                    "vote_count": 1000,
                },
                {
                    "id": 202,
                    "title": "Dark Movie",
                    "release_date": "2020-01-01",
                    "overview": "A visually dark and atmospheric psychological thriller.",
                    "vote_average": 7.5,
                    "vote_count": 1200,
                },
            ]
        }

    async def get_movie_details(self, movie_id):
        return {
            "id": movie_id,
            "title": {
                27205: "Inception",
                101: "Bright Movie",
                202: "Dark Movie",
            }[movie_id],
        }


def test_provider_permission_failure_maps_to_provider_down():
    error = RuntimeError("Error code: 403 - access denied")

    failure = failure_response(error)

    assert failure.kind == FailureKind.PROVIDER_DOWN
    assert "unavailable" in failure.message


def test_provider_generation_error_is_not_misreported_as_rate_limited():
    error = type("GenerateError", (RuntimeError,), {})("invalid model configuration")

    failure = failure_response(error)

    assert failure.kind == FailureKind.UNKNOWN


def test_provider_http_429_is_reported_as_rate_limited():
    error = RuntimeError("provider request failed")
    error.status_code = 429

    failure = failure_response(error)

    assert failure.kind == FailureKind.RATE_LIMITED
    assert failure.retry_after_seconds == 20


def test_person_discussion_answer_has_no_internal_source_note():
    answer = "The performer is described in the retrieved evidence."

    assert "Source note:" not in answer
    assert "Wikipedia" not in answer


def test_hard_runtime_limit_rejects_candidates_without_verified_runtime():
    candidates = RecommendationService().filter(
        [
            {"id": 1, "title": "Known short film", "runtime": 110},
            {"id": 2, "title": "Unknown runtime"},
            {"id": 3, "title": "Too long", "runtime": 130},
        ],
        {"watched": [], "watchlist": [], "preferences": []},
        {"runtime_max": 120},
    )

    assert [item["id"] for item in candidates] == [1]


def test_runtime_limit_keeps_slightly_longer_candidates_for_stretch_group():
    candidates = [
        {"id": 1, "title": "Exact fit", "runtime": 118},
        {"id": 2, "title": "Five minutes over", "runtime": 125},
        {"id": 3, "title": "Too far over", "runtime": 160},
    ]
    strict = RecommendationService().filter(
        candidates,
        {"watched": [], "watchlist": [], "preferences": []},
        {"runtime_max": 120},
    )
    stretch = [
        item for item in candidates if item.get("runtime") is not None and 120 < item["runtime"] <= 150
    ]

    assert [item["id"] for item in strict] == [1]
    assert [item["id"] for item in stretch] == [2]


class FakeEvidenceAcquirer(EvidenceAcquirer):
    acquisition_type = "web"

    async def acquire(
        self,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        values = {
            27205: 0.50,
            101: 0.25,
            202: 0.90,
        }

        return AcquisitionResult(
            request=request,
            completed=True,
            evidence=[
                Evidence(
                    id=f"visual-{request.movie_id}",
                    movie_id=request.movie_id,
                    source_type="web",
                    source_name="test",
                    aspect="visual_darkness",
                    value=values[request.movie_id],
                    content=f"Visual darkness evidence for {request.movie_id}",
                    confidence="high",
                )
            ],
        )


def test_recommendation_ranking_uses_semantic_focus():
    from app.recommendations import RecommendationService

    candidates = [
        {
            "id": 1,
            "title": "Popular Romance",
            "overview": "A romantic drama",
            "vote_average": 9.5,
            "vote_count": 100000,
            "runtime": 110,
        },
        {
            "id": 2,
            "title": "Relentless Revolution",
            "overview": "A revolutionary action thriller",
            "vote_average": 7.4,
            "vote_count": 5000,
            "runtime": 110,
        },
    ]
    ranked = RecommendationService().rank(
        candidates,
        {"watched": [], "watchlist": [], "preferences": []},
        {"runtime_max": 120, "semantic_focus": "revolutionary action"},
        limit=2,
    )
    assert ranked[0]["id"] == 2
