from app.agent.tools import MovieTools
from app.recommendations import RecommendationService


def movie(
    id,
    title="Movie",
    rating=7.0,
    votes=1000,
    runtime=110,
    year="2020-01-01",
    genres=None,
):
    return {
        "id": id,
        "title": title,
        "vote_average": rating,
        "vote_count": votes,
        "runtime": runtime,
        "release_date": year,
        "genre_ids": genres or [],
        "overview": "",
        "tagline": "",
    }


def test_filter_excludes_watched_and_watchlisted_movies():
    svc = RecommendationService()
    candidates = [movie(1), movie(2), movie(3)]
    profile = {"watched": [{"tmdb_id": 1}], "watchlist": [2]}
    out = svc.filter(candidates, profile)
    assert {c["id"] for c in out} == {3}


def test_filter_enforces_runtime_constraints():
    svc = RecommendationService()
    candidates = [movie(1, runtime=90), movie(2, runtime=150), movie(3, runtime=None)]
    profile = {"watched": [], "watchlist": []}
    out = svc.filter(candidates, profile, {"runtime_max": 120})
    assert {c["id"] for c in out} == {1}  # too-long and unknown-runtime movies excluded


def test_filter_enforces_release_year_range():
    svc = RecommendationService()
    candidates = [
        movie(1, year="1990-01-01"),
        movie(2, year="2015-01-01"),
        movie(3, year="2023-01-01"),
    ]
    profile = {"watched": [], "watchlist": []}
    out = svc.filter(candidates, profile, {"release_year_min": 2000, "release_year_max": 2020})
    assert {c["id"] for c in out} == {2}


def test_rank_orders_higher_rated_movies_first_all_else_equal():
    svc = RecommendationService()
    candidates = [movie(1, rating=6.0, votes=1000), movie(2, rating=9.0, votes=1000)]
    profile = {"watched": [], "watchlist": [], "preferences": []}
    ranked = svc.rank(candidates, profile)
    assert ranked[0]["id"] == 2


def test_rank_boosts_movies_matching_semantic_focus():
    svc = RecommendationService()
    candidates = [
        movie(1, title="A quiet family drama", rating=7.0, votes=1000),
        movie(2, title="Explosive time travel heist", rating=7.0, votes=1000),
    ]
    candidates[1]["overview"] = "A thrilling time travel heist across decades."
    profile = {"watched": [], "watchlist": [], "preferences": []}
    ranked = svc.rank(candidates, profile, {"semantic_focus": "time travel heist"})
    assert ranked[0]["id"] == 2


def test_rank_applies_preference_boost_and_avoid_penalty():
    svc = RecommendationService()
    candidates = [
        movie(1, title="A gritty horror film", rating=7.0, votes=1000),
        movie(2, title="A gentle comedy", rating=7.0, votes=1000),
    ]
    profile = {
        "watched": [],
        "watchlist": [],
        "preferences": [{"value": "horror", "polarity": "avoid", "strength": 1.0, "confidence": 1.0}],
    }
    ranked = svc.rank(candidates, profile)
    assert ranked[0]["id"] == 2  # horror penalized, comedy ranks first


def test_rank_does_not_match_partial_preference_words():
    svc = RecommendationService()
    candidates = [
        movie(1, title="A party film", rating=7.0, votes=1000),
        movie(2, title="A quiet art film", rating=7.0, votes=1000),
    ]
    profile = {
        "watched": [],
        "watchlist": [],
        "preferences": [{"value": "art", "polarity": "prefer", "strength": 1.0, "confidence": 1.0}],
    }
    ranked = svc.rank(candidates, profile)
    assert ranked[0]["id"] == 2


def test_rank_uses_shared_genres_from_ratings_as_bounded_signal():
    svc = RecommendationService()
    candidates = [
        movie(1, title="Unrelated", rating=7.5, genres=[18]),
        movie(2, title="Genre match", rating=7.0, genres=[28]),
    ]
    profile = {
        "watched": [],
        "watchlist": [],
        "ratings": [
            {"movie": {"tmdb_id": 101, "genre_ids": [28]}, "rating": 10},
            {"movie": {"tmdb_id": 102, "genre_ids": [28]}, "rating": 9},
        ],
        "preferences": [],
    }
    ranked = svc.rank(candidates, profile)
    assert ranked[0]["id"] == 2


def test_filter_uses_complete_exclusion_ids_beyond_display_profile_limit():
    svc = RecommendationService()
    profile = {
        "watched": [],
        "watchlist": [],
        "all_watched_tmdb_ids": [99],
        "all_watchlist_tmdb_ids": [100],
    }
    out = svc.filter([movie(99), movie(100), movie(101)], profile)
    assert [item["id"] for item in out] == [101]


def test_public_movie_results_use_profile_aware_filtering():
    payload = {"reference_movie_id": 7, "results": [movie(1), movie(2)]}
    profile = {"watched": [{"tmdb_id": 1}], "watchlist": []}
    personalized = MovieTools.personalize_movie_results(payload, profile)
    assert [item["id"] for item in personalized["results"]] == [2]


def test_rank_respects_limit():
    svc = RecommendationService()
    candidates = [movie(i, rating=5 + i * 0.1) for i in range(20)]
    profile = {"watched": [], "watchlist": [], "preferences": []}
    ranked = svc.rank(candidates, profile, limit=5)
    assert len(ranked) == 5


def test_rank_diversifies_across_genres():
    svc = RecommendationService()
    # Ten blockbusters sharing the same 3-genre combo, all rated slightly
    # higher than a single differently-genred drama. Real TMDB movies
    # typically carry 2-4 genre tags each, which is what actually lets the
    # ">2 shared genres" diversity penalty engage; a single shared genre
    # tag (as in a naive single-genre test) never crosses that threshold.
    candidates = [movie(i, rating=8.0, genres=[28, 12, 878]) for i in range(10)]
    candidates.append(movie(99, rating=7.9, genres=[18]))
    profile = {"watched": [], "watchlist": [], "preferences": []}
    ranked = svc.rank(candidates, profile, limit=4)
    genres_present = {tuple(c["genre_ids"]) for c in ranked}
    assert (18,) in genres_present  # the differently-genred movie got pulled in


def test_finalize_boosts_movies_with_more_review_evidence():
    svc = RecommendationService()
    candidates = [movie(1), movie(2)]
    for c in candidates:
        c["structured_score"] = 0.5
        c["diversity_adjusted_score"] = 0.5
    review_evidence = [
        {
            "movie_id": 1,
            "evidence_review_count": 5,
            "aspect_summary": {"craft": "x", "story": "y"},
        },
        {"movie_id": 2, "evidence_review_count": 0, "aspect_summary": {}},
    ]
    ranked = svc.finalize(candidates, review_evidence=review_evidence)
    assert ranked[0]["id"] == 1
    assert ranked[0]["evidence_quality_score"] > ranked[1]["evidence_quality_score"]


def test_finalize_handles_missing_evidence_gracefully():
    svc = RecommendationService()
    candidates = [movie(1)]
    candidates[0]["structured_score"] = 0.5
    ranked = svc.finalize(candidates)
    assert ranked[0]["evidence_quality_score"] == 0.0
