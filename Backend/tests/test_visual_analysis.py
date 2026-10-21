from types import SimpleNamespace

import pytest

from app.agent.router import QueryClassification
from app.agent.tools import fetch_visual_analysis


class FakeTMDB:
    async def search_movies(self, title):
        assert title == "Test Film"
        return {
            "results": [
                {"id": 1, "title": "Another Film"},
                {"id": 2, "title": "Test Film"},
            ]
        }

    async def get_movie_details(self, movie_id):
        assert movie_id == 2
        return {
            "title": "Test Film",
            "credits": {
                "crew": [
                    {"name": "Cinematographer One", "job": "Director of Photography"},
                    {"name": "Cinematographer Two", "job": "Cinematography"},
                    {"name": "VFX Supervisor", "job": "Visual Effects Supervisor"},
                ]
            },
            "production_companies": [
                {"name": "Unlabeled Studio"},
                {"name": "VFX Studio", "type": "Visual Effects"},
            ],
        }


class FakeWeb:
    def __init__(self):
        self.query = None

    async def search(self, query):
        self.query = query
        return {
            "results": [
                {
                    "title": "Test Film cinematography interview",
                    "url": "https://example.com/cinematography",
                    "snippet": "Shot on 35mm film with anamorphic lenses and a warm palette.",
                }
            ]
        }


class FakeProvider:
    async def generate(self, request):
        self.request = request
        return SimpleNamespace(text="**🎥 Visual & Technical Analysis**\nGrounded analysis.")


class FakeRouter:
    def __init__(self):
        self.provider = FakeProvider()

    def fast(self):
        return self.provider


@pytest.mark.asyncio
async def test_fetch_visual_analysis_uses_exact_tmdb_match_and_grounded_results():
    web = FakeWeb()
    router = FakeRouter()

    result = await fetch_visual_analysis(
        "Test Film",
        tmdb=FakeTMDB(),
        web=web,
        router=router,
    )

    assert web.query == (
        "Test Film cinematography Cinematographer One, Cinematographer Two "
        "camera format lenses color palette"
    )
    assert result == "🎥-visual-&-technical-analysis, grounded-analysis."
    request = router.provider.request
    system = request.messages[0]["content"]
    user = request.messages[1]["content"]
    assert "Using ONLY the provided search results and metadata" in system
    assert "flat, comma-separated" in system
    assert "Cinematographer One" in user
    assert "VFX Studio" in user
    assert "Unlabeled Studio" in user
    assert "Shot on 35mm film" in user


@pytest.mark.asyncio
async def test_fetch_visual_analysis_returns_graceful_message_on_search_timeout(
    monkeypatch,
):
    class TimedOutWeb:
        async def search(self, _query):
            raise TimeoutError("search timed out")

        async def close(self):
            pass

    result = await fetch_visual_analysis(
        "Test Film",
        tmdb=FakeTMDB(),
        web=TimedOutWeb(),
    )

    assert result == "Could not retrieve grounded visual analysis for **Test Film** right now."


def test_visual_analysis_is_a_movie_router_intent():
    classification = QueryClassification(
        domain="movie",
        intent="visual_analysis",
        needs_live_search=False,
        movie_title="Test Film",
    )

    assert classification.intent == "visual_analysis"
