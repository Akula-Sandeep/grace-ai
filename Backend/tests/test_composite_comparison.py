import pytest

from app.agent.composite import run_comparison
from app.agent.planner import ExecutionPlanner

FILMS = {
    "Arrival": {
        "id": 329865,
        "title": "Arrival",
        "release_date": "2016-11-10",
        "runtime": 116,
        "vote_average": 7.6,
    },
    "Interstellar": {
        "id": 157336,
        "title": "Interstellar",
        "release_date": "2014-11-05",
        "runtime": 169,
        "vote_average": 8.4,
    },
    "Contact": {
        "id": 686,
        "title": "Contact",
        "release_date": "1997-07-11",
        "runtime": 150,
        "vote_average": 7.4,
    },
}


class FakeTools:
    def __init__(self):
        self.searches = []

    async def search_movies(self, query, page=1):
        self.searches.append(query)
        film = FILMS.get(query)
        return {"results": [film] if film else [{"id": 1, "title": "Something Else", "release_date": "2000"}]}

    async def get_movie_details(self, movie_id, fresh=False):
        return next(f for f in FILMS.values() if f["id"] == movie_id)

    async def close(self):
        pass


class FakeWeb:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if "music" in query and "Contact" in query:
            return {"results": []}
        return {
            "results": [
                {
                    "title": "Variety",
                    "url": f"https://variety.com/{len(self.queries)}",
                    "snippet": f"Evidence about {query}.",
                }
            ]
        }


def test_plan_runs_every_film_and_aspect_in_parallel_with_budgets():
    plan = ExecutionPlanner().comparison_plan(
        ["A", "B", "C"], ["music and score", "pacing and structure"], web_available=True
    )
    ids = [t.id for t in plan.tasks]
    assert ids[0] == "resolve_references"
    assert {"details_0", "details_1", "details_2"} <= set(ids)
    assert sum(t.type == "acquire_movie_evidence" for t in plan.tasks) == 6
    assert plan.max_web_searches == 6 and plan.max_tool_calls == 7
    assert all(not t.required for t in plan.tasks[1:])


@pytest.mark.asyncio
async def test_comparison_gathers_per_film_per_aspect_evidence_and_reports_gaps():
    web = FakeWeb()
    evidence = await run_comparison(
        ["Arrival", "Interstellar", "Contact", "Nonexistent Film"],
        ["music and score", "critical reception"],
        tools=FakeTools(),
        web_client=web,
    )
    titles = [f["title"] for f in evidence["films"]]
    assert titles == ["Arrival", "Interstellar", "Contact"]
    assert evidence["unresolved_films"][0]["requested"] == "Nonexistent Film"
    arrival = evidence["films"][0]
    assert set(arrival["evidence"]) == {"music and score", "critical reception"}
    assert arrival["evidence"]["music and score"][0]["url"].startswith("https://variety.com/")
    contact = evidence["films"][2]
    assert contact["missing_evidence"] == ["music and score"]
    assert len(web.queries) == 6  # 3 resolved films x 2 aspects, in one plan
