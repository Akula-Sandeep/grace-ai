"""Person pages (UI spec §51/§108)."""

import pytest

from app.services.person_intelligence import build_filmography, build_person


class FakeTMDB:
    def __init__(self):
        self.movies = {
            1: {
                "credits": {
                    "cast": [{"id": 50, "name": "Co Star"}],
                    "crew": [{"id": 60, "name": "D. Op", "job": "Director of Photography"}],
                }
            },
            2: {
                "credits": {
                    "cast": [{"id": 50, "name": "Co Star"}],
                    "crew": [{"id": 60, "name": "D. Op", "job": "Director of Photography"}],
                }
            },
            3: {"credits": {"cast": [{"id": 70, "name": "One Timer"}], "crew": []}},
        }

    async def _get(self, path, params=None):
        assert path == "/person/7"
        return {
            "id": 7,
            "name": "A. Director",
            "known_for_department": "Directing",
            "biography": "Makes films.",
            "external_ids": {"wikidata_id": "Q1"},
            "movie_credits": {
                "cast": [
                    {
                        "id": 3,
                        "title": "Cameo",
                        "release_date": "2001-01-01",
                        "character": "Himself",
                        "popularity": 1,
                    }
                ],
                "crew": [
                    {
                        "id": 1,
                        "title": "Big Film",
                        "release_date": "2010-05-01",
                        "job": "Director",
                        "department": "Directing",
                        "popularity": 90,
                    },
                    {
                        "id": 1,
                        "title": "Big Film",
                        "release_date": "2010-05-01",
                        "job": "Writer",
                        "department": "Directing",
                        "popularity": 90,
                    },
                    {
                        "id": 2,
                        "title": "Newer Film",
                        "release_date": "2020-05-01",
                        "job": "Director",
                        "department": "Directing",
                        "popularity": 50,
                    },
                ],
            },
        }

    async def get_movie_details(self, movie_id):
        return self.movies[movie_id]


def test_filmography_groups_by_role_newest_first_and_merges_jobs():
    films = build_filmography(
        {
            "crew": [
                {
                    "id": 1,
                    "title": "Old",
                    "release_date": "2000-01-01",
                    "job": "Director",
                    "department": "Directing",
                },
                {
                    "id": 2,
                    "title": "New",
                    "release_date": "2020-01-01",
                    "job": "Director",
                    "department": "Directing",
                },
                {
                    "id": 2,
                    "title": "New",
                    "release_date": "2020-01-01",
                    "job": "Producer",
                    "department": "Directing",
                },
            ]
        }
    )
    assert [c["title"] for c in films["Directing"]] == ["New", "Old"]
    assert films["Directing"][0]["role"] == "Director, Producer"


@pytest.mark.asyncio
async def test_person_page_has_awards_and_evidence_backed_collaborators():
    async def awards(wikidata_id):
        assert wikidata_id == "Q1"
        return [{"result": "Won", "award": "Best Director", "year": "2011", "source": "Wikidata P166"}]

    person = await build_person(7, tmdb=FakeTMDB(), awards_lookup=awards)
    assert person["name"] == "A. Director"
    assert list(person["filmography"]) == ["Directing", "Acting"]
    assert person["known_for"][0]["title"] == "Big Film"
    assert person["awards"][0]["result"] == "Won"
    names = {c["name"]: c for c in person["collaborators"]}
    assert set(names) == {"Co Star", "D. Op"}  # appear in 2 shared films; the one-timer does not
    assert sorted(names["D. Op"]["shared_titles"]) == ["Big Film", "Newer Film"]
    assert "can be incomplete" in person["coverage"]
