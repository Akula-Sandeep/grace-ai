import pytest

from app.agent.composite import semantic_rerank
from app.agent.router import QueryClassification
from app.knowledge.evidence_interpreter import EvidenceInterpretation, EvidenceValueAssessment


class Web:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return {
            "results": [
                {"title": "Review", "url": f"https://example.org/{len(self.queries)}", "snippet": query}
            ]
        }


class Interpreter:
    """Stands in for the LLM interpreter: 'Rainy Film' evidence supports rain."""

    async def interpret(self, requirement, evidence):
        return [
            item.model_copy(update={"value": requirement.desired_value if "Rainy" in item.content else "no"})
            for item in evidence
        ]


STRICT = [
    {"tmdb_id": 1, "title": "Sunny Film", "release_date": "2010-01-01"},
    {"tmdb_id": 2, "title": "Rainy Film", "release_date": "2012-01-01"},
]


@pytest.mark.asyncio
async def test_evidence_moves_the_film_that_actually_has_rain_up():
    web = Web()
    result = await semantic_rerank(
        {"strict_matches": STRICT},
        QueryClassification(
            domain="movie", intent="recommendation", needs_live_search=False, mood_keywords=["rain"]
        ),
        tools=None,
        web_client=web,
        interpreter=Interpreter(),
    )
    assert [m["title"] for m in result["strict_matches"]] == ["Rainy Film", "Sunny Film"]
    assert result["strict_matches"][0]["evidence_assessment"]["supported"] == ["rain"]
    assert result["strict_matches"][1]["evidence_assessment"]["contrary"] == ["rain"]
    assert result["strict_matches"][0]["evidence_assessment"]["findings"][0]["url"].startswith(
        "https://example.org/"
    )
    assert result["semantic_evidence"]["status"] == "applied"
    assert len(web.queries) == 2


@pytest.mark.asyncio
async def test_without_web_or_soft_requirements_the_engine_order_stands():
    plain = QueryClassification(domain="movie", intent="recommendation", needs_live_search=False)
    assert (
        await semantic_rerank(
            {"strict_matches": STRICT}, plain, tools=None, web_client=Web(), interpreter=None
        )
    )["strict_matches"] == STRICT
    moody = plain.model_copy(update={"mood_keywords": ["rain"]})
    assert (
        await semantic_rerank(
            {"strict_matches": STRICT}, moody, tools=None, web_client=None, interpreter=None
        )
    )["strict_matches"] == STRICT


def test_interpreter_schema_is_importable():
    assert EvidenceInterpretation(assessments=[EvidenceValueAssessment(evidence_id="x", value="rain")])
