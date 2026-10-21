import asyncio

from app.agent.tools import MovieTools


class FakeTMDB:
    async def get_movie_details(self, movie_id):
        return {
            "id": movie_id,
            "title": "Rain City",
            "overview": "",
            "reviews": {
                "results": [
                    {
                        "author": "A",
                        "content": "The cinematography is beautiful and the acting is strong.",
                        "url": "u1",
                    },
                    {
                        "author": "B",
                        "content": "The cinematography is beautiful but the pacing is slow.",
                        "url": "u2",
                    },
                    {
                        "author": "C",
                        "content": "The acting is weak and the story is confusing.",
                        "url": "u3",
                    },
                ]
            },
        }

    async def close(self):
        pass


class FakeWeb:
    async def search(self, q):
        return {
            "results": [
                {
                    "title": "Rain City awards",
                    "url": "https://oscars.org/example",
                    "snippet": "Rain City won an award at the festival.",
                }
            ]
        }


async def run():
    t = MovieTools(tmdb=FakeTMDB(), web=FakeWeb())
    r = await t.review_intelligence(1, "craft,acting,story")
    assert r["aspect_summary"]["craft"]["review_count"] == 2
    assert r["aspect_summary"]["acting"]["supporting"] == 1
    assert r["aspect_summary"]["acting"]["critical"] == 1
    assert r["aspect_summary"]["story"]["critical"] == 1
    m = await t.movie_intelligence(1, "awards")
    assert m["research_queries"] and m["evidence"] and m["source_map"]["structured"] == "TMDB"


def test_intelligence_aggregation_contract():
    asyncio.run(run())


def test_movie_search_merges_local_cache_with_tmdb_and_keeps_tmdb_authoritative():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base, Movie

    class SearchTMDB:
        async def search_movies(self, query, page=1):
            return {
                "total_results": 2,
                "results": [
                    {
                        "id": 101,
                        "title": "Example Movie",
                        "original_title": "Example Movie",
                        "release_date": "2025-01-01",
                        "overview": "Current overview",
                        "vote_average": 8.4,
                        "vote_count": 1000,
                        "popularity": 90.0,
                        "poster_path": "/current.jpg",
                        "backdrop_path": "/current-bg.jpg",
                        "genre_ids": [18],
                    },
                    {
                        "id": 202,
                        "title": "Another Movie",
                        "original_title": "Another Movie",
                        "release_date": "2024-01-01",
                        "overview": "Upstream-only movie",
                        "vote_average": 7.0,
                        "vote_count": 100,
                        "popularity": 50.0,
                        "poster_path": "/another.jpg",
                        "backdrop_path": None,
                        "genre_ids": [18],
                    },
                ],
            }

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(
            Movie(
                tmdb_id=101,
                title="Example Movie",
                original_title="Example Movie",
                release_year=2023,
                overview="Stale local overview",
                poster_path="/old.jpg",
                metadata_json={
                    "vote_average": 7.1,
                    "vote_count": 10,
                    "popularity": 42.0,
                    "genre_ids": [28],
                },
            )
        )
        db.commit()
        tools = MovieTools(tmdb=SearchTMDB(), web=FakeWeb(), db=db)

        import asyncio

        result = asyncio.run(tools.search_movies("Example Movie"))
        rows = {item["id"]: item for item in result["results"]}

        assert set(rows) == {101, 202}
        assert rows[101]["overview"] == "Current overview"
        assert rows[101]["popularity"] == 90.0
        assert rows[101]["poster_path"] == "/current.jpg"
        assert result["source_policy"] == "tmdb_authoritative_with_local_cache_boost"
    finally:
        db.close()
