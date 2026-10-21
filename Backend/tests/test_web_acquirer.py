import pytest

from app.knowledge.acquisition import (
    AcquisitionRequest,
    WebMovieAcquirer,
)


class FakeWebClient:
    def __init__(self, results):
        self.results = results
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return self.results


@pytest.mark.asyncio
async def test_web_acquirer_creates_evidence():
    client = FakeWebClient(
        {
            "results": [
                {
                    "title": "Inception review",
                    "url": "https://example.com/inception",
                    "content": ("The film frequently uses dark lighting " "and subdued visual compositions."),
                }
            ]
        }
    )

    acquirer = WebMovieAcquirer(client)

    request = AcquisitionRequest(
        requirement_id="visual_darkness",
        movie_id=27205,
        aspect="visual_darkness",
        acquisition_type="web",
        query="Inception visual darkness cinematography",
    )

    result = await acquirer.acquire(request)

    assert result.completed is True
    assert result.source_count == 1
    assert len(result.evidence) == 1

    evidence = result.evidence[0]

    assert evidence.movie_id == 27205
    assert evidence.source_type == "web"
    assert evidence.aspect == "visual_darkness"
    assert evidence.source_url == ("https://example.com/inception")


@pytest.mark.asyncio
async def test_web_acquirer_uses_request_query():
    client = FakeWebClient({"results": []})

    acquirer = WebMovieAcquirer(client)

    request = AcquisitionRequest(
        requirement_id="visual_darkness",
        movie_id=27205,
        aspect="visual_darkness",
        acquisition_type="web",
        query="Inception dark cinematography",
    )

    await acquirer.acquire(request)

    assert client.queries == ["Inception dark cinematography"]


@pytest.mark.asyncio
async def test_web_acquirer_returns_exhausted_when_no_results():
    client = FakeWebClient({"results": []})

    acquirer = WebMovieAcquirer(client)

    request = AcquisitionRequest(
        requirement_id="visual_darkness",
        movie_id=27205,
        aspect="visual_darkness",
        acquisition_type="web",
    )

    result = await acquirer.acquire(request)

    assert result.completed is False
    assert result.exhausted is True
    assert result.error is not None


@pytest.mark.asyncio
async def test_web_acquirer_handles_search_failure():
    class FailingClient:
        async def search(self, query):
            raise RuntimeError("search failed")

    acquirer = WebMovieAcquirer(FailingClient())

    request = AcquisitionRequest(
        requirement_id="visual_darkness",
        movie_id=27205,
        aspect="visual_darkness",
        acquisition_type="web",
        query="Inception visual darkness",
    )

    result = await acquirer.acquire(request)

    assert result.completed is False
    assert result.exhausted is False
    assert result.error == "search failed"
