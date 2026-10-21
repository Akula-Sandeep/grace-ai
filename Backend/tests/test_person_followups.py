import pytest

from app.agent.router import QueryClassification
from app.services.movie_data_fallback import MovieDataFallbackEngine, _compact_credits


def test_compact_credits_keep_role_genre_and_year():
    crew = _compact_credits(
        [
            {
                "id": 1,
                "title": "Interstellar",
                "release_date": "2014-11-05",
                "genre_ids": [878, 18],
                "job": "Director",
            },
            {
                "id": 2,
                "title": "Tenet",
                "release_date": "2020-08-22",
                "genre_ids": [28, 878],
                "job": "Director",
            },
            {"id": 3, "title": "", "job": "Producer"},
        ],
        "crew",
    )
    assert [c["title"] for c in crew] == ["Tenet", "Interstellar"]
    assert crew[1] == {
        "id": 1,
        "title": "Interstellar",
        "year": "2014",
        "genres": ["Science Fiction", "Drama"],
        "job": "Director",
    }


class _TMDB:
    async def _get(self, path, params=None):
        if path == "/search/person":
            return {
                "results": [{"id": 525, "name": "Christopher Nolan", "known_for_department": "Directing"}]
            }
        return {
            "biography": "",
            "movie_credits": {
                "cast": [],
                "crew": [
                    {
                        "id": 157336,
                        "title": "Interstellar",
                        "release_date": "2014-11-05",
                        "genre_ids": [878],
                        "job": "Director",
                    }
                ],
            },
        }


@pytest.mark.asyncio
async def test_director_lookup_includes_directing_credits(monkeypatch):
    engine = MovieDataFallbackEngine.__new__(MovieDataFallbackEngine)
    monkeypatch.setattr(engine, "_tmdb_client", lambda: _TMDB(), raising=False)

    async def leaf(function, *args, **kwargs):
        return await function(*args, **kwargs)

    monkeypatch.setattr(engine, "_leaf", leaf, raising=False)
    classification = QueryClassification(
        domain="movie", intent="cast_crew", needs_live_search=False, person_name="Christopher Nolan"
    )
    result = await engine.lookup_query(classification, "what about his other sci-fi movies?")
    assert result["status"] == "found"
    assert result["crew_credits"][0]["job"] == "Director"
    assert result["crew_credits"][0]["genres"] == ["Science Fiction"]
