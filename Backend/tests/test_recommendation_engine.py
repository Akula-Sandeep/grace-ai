"""Constraint safety for recommendations (Jarvis spec §4-§7, product spec §26/§49)."""

import random

import pytest

from app.agent.router import QueryClassification
from app.services.recommendation_engine import Constraints, recommend


def movie(
    mid,
    title,
    runtime,
    *,
    genres=(878,),
    year=2015,
    votes=2000,
    popularity=40.0,
    rating=7.5,
    lang="en",
    overview="",
    keywords=(),
):
    return {
        "id": mid,
        "title": title,
        "runtime": runtime,
        "genre_ids": list(genres),
        "genres": [{"id": g} for g in genres],
        "release_date": f"{year}-06-01",
        "vote_count": votes,
        "popularity": popularity,
        "vote_average": rating,
        "original_language": lang,
        "overview": overview,
        "keywords": {"keywords": [{"name": k} for k in keywords]},
    }


class FakeTMDB:
    def __init__(self, catalog, *, recommendations=None):
        self.catalog = {m["id"]: m for m in catalog}
        self.recommendations = recommendations or {}
        self.discover_calls = []

    async def search_movies(self, query, page=1):
        hits = [m for m in self.catalog.values() if m["title"].casefold() == query.casefold()]
        return {"results": [{k: v for k, v in m.items() if k != "runtime"} for m in hits]}

    async def discover_movies(self, **kwargs):
        self.discover_calls.append(kwargs)
        # List results never carry runtime, exactly like TMDB /discover.
        return {"results": [{k: v for k, v in m.items() if k != "runtime"} for m in self.catalog.values()]}

    async def get_recommendations(self, movie_id, page=1):
        return {"results": [self.catalog[i] for i in self.recommendations.get(movie_id, [])]}

    async def get_similar_movies(self, movie_id, page=1):
        return {"results": []}

    async def get_movie_details(self, movie_id):
        return dict(self.catalog[movie_id])

    async def get_watch_providers(self, movie_id, region=None):
        return {"flatrate": [{"provider_name": "Example+"}], "region": region}


SPEC_EXAMPLE = QueryClassification(
    domain="movie",
    intent="recommendation",
    needs_live_search=False,
    max_runtime_minutes=120,
    mood_keywords=["dark", "rain"],
    avoid_keywords=["devastating"],
    reference_titles=["Interstellar"],
    prefer_less_popular=True,
)


def catalog():
    return [
        movie(157336, "Interstellar", 169, votes=35000, popularity=200),
        movie(
            1,
            "Rain Signal",
            104,
            overview="A dark, rain-soaked mystery on a remote station.",
            votes=900,
            popularity=12,
            keywords=("rain", "dark"),
        ),
        movie(
            2,
            "Big Blockbuster",
            115,
            votes=30000,
            popularity=400,
            overview="Explosions.",
        ),
        movie(3, "Long Odyssey", 128, overview="A dark voyage.", keywords=("dark",)),
        movie(4, "Watched Already", 100, overview="Dark rain."),
        movie(5, "Far Too Long", 160),
        movie(
            6,
            "Grief Machine",
            110,
            overview="An emotionally devastating story.",
            keywords=("devastating",),
        ),
        movie(7, "No Runtime", None),
    ]


@pytest.mark.asyncio
async def test_spec_example_respects_hard_constraints_and_labels_near_matches():
    tmdb = FakeTMDB(catalog(), recommendations={157336: [1, 3]})
    result = await recommend(SPEC_EXAMPLE, tmdb=tmdb, exclude_tmdb_ids={4}, region="IN")

    strict_ids = [m["tmdb_id"] for m in result["strict_matches"]]
    assert strict_ids, result
    for match in result["strict_matches"]:
        assert match["runtime"] is not None and match["runtime"] <= 120
        assert match["tmdb_id"] not in {4, 157336}
    assert 7 not in strict_ids  # runtime cannot be verified
    assert strict_ids[0] == 1  # dark + rain + reference-adjacent + not a blockbuster
    assert strict_ids.index(1) < strict_ids.index(2)
    assert strict_ids.index(1) < strict_ids.index(6)  # "devastating" is steered away from, not banned
    assert all(m["tmdb_id"] != 5 for m in result["near_matches"])  # 40 min over is never offered
    near = {m["tmdb_id"]: m for m in result["near_matches"]}
    if 3 in near:
        assert near[3]["relaxed_constraint"] == {
            "constraint": "runtime",
            "limit_minutes": 120,
            "runtime_minutes": 128,
            "over_by_minutes": 8,
        }
    assert "runtime at most 120 minutes" in result["hard_constraints_applied"]
    assert result["strict_matches"][0]["watch_providers"]["region"] == "IN"
    assert all("mood_keywords" not in call for call in tmdb.discover_calls)  # moods never filter


@pytest.mark.asyncio
async def test_genre_language_year_and_veto_are_hard_filters():
    items = [
        movie(10, "Korean Thriller", 110, genres=(53,), lang="ko", year=2019),
        movie(11, "English Thriller", 110, genres=(53,), lang="en", year=2019),
        movie(12, "Korean Horror Thriller", 110, genres=(53, 27), lang="ko", year=2019),
        movie(13, "Old Korean Thriller", 110, genres=(53,), lang="ko", year=2001),
    ]
    request = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        genres=["Thriller"],
        exclude_genres=["horror"],
        original_language="ko",
        min_year=2010,
    )
    result = await recommend(request, tmdb=FakeTMDB(items))
    assert [m["tmdb_id"] for m in result["strict_matches"]] == [10]
    assert result["near_matches"] == []


@pytest.mark.asyncio
async def test_no_candidates_is_reported_not_invented():
    request = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        max_runtime_minutes=60,
    )
    result = await recommend(request, tmdb=FakeTMDB([movie(20, "Epic", 190)]))
    assert result["status"] == "no_verified_candidates"
    assert result["strict_matches"] == [] and result["near_matches"] == []


def test_approximate_runtime_becomes_a_window():
    request = QueryClassification(
        domain="movie",
        intent="recommendation",
        needs_live_search=False,
        max_runtime_minutes=120,
        runtime_is_approximate=True,
    )
    constraints = Constraints.from_classification(request)
    assert (constraints.runtime_min, constraints.runtime_max) == (100, 130)


@pytest.mark.asyncio
async def test_property_strict_matches_never_violate_hard_constraints():
    rng = random.Random(7)  # noqa: S311 - deterministic test data, not cryptography
    for trial in range(40):
        items = [
            movie(
                100 + i,
                f"Film {trial}-{i}",
                rng.choice([None, 80, 95, 110, 119, 120, 121, 130, 135, 150]),
                genres=tuple(rng.sample([18, 27, 35, 53, 878], k=rng.randint(1, 3))),
                year=rng.randint(1980, 2024),
                lang=rng.choice(["en", "ko", "fr"]),
            )
            for i in range(12)
        ]
        request = QueryClassification(
            domain="movie",
            intent="recommendation",
            needs_live_search=False,
            max_runtime_minutes=rng.choice([None, 100, 120]),
            min_year=rng.choice([None, 2000]),
            exclude_genres=rng.choice([None, ["Horror"]]),
            original_language=rng.choice([None, "en"]),
        )
        excluded = {100 + rng.randrange(12)}
        result = await recommend(request, tmdb=FakeTMDB(items), exclude_tmdb_ids=excluded)
        constraints = Constraints.from_classification(request, excluded)
        by_id = {m["id"]: m for m in items}
        for match in result["strict_matches"]:
            assert constraints.violations(by_id[match["tmdb_id"]]) == []
        for match in result["near_matches"]:
            assert match["relaxed_constraint"]["over_by_minutes"] <= 15
