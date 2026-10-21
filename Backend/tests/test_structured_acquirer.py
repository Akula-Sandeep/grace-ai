import pytest

from app.knowledge.acquisition.models import AcquisitionRequest
from app.knowledge.acquisition.structured import (
    StructuredMovieAcquirer,
)


class FakeMovieTools:
    def __init__(self, movie=None, error=None):
        self.movie = movie
        self.error = error

    async def get_movie_details(self, movie_id):
        if self.error:
            raise self.error

        return self.movie


def make_request(
    aspect="runtime",
    movie_id=27205,
):
    return AcquisitionRequest(
        requirement_id="runtime",
        movie_id=movie_id,
        aspect=aspect,
        acquisition_type="structured",
    )


@pytest.mark.asyncio
async def test_acquires_structured_evidence():
    tools = FakeMovieTools(
        movie={
            "id": 27205,
            "title": "Inception",
            "runtime": 148,
            "release_date": "2010-07-16",
            "original_language": "en",
        }
    )

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(make_request("runtime"))

    assert result.completed is True
    assert result.exhausted is False
    assert result.error is None
    assert result.source_count == 1
    assert len(result.evidence) == 1

    evidence = result.evidence[0]

    assert evidence.movie_id == 27205
    assert evidence.source_type == "structured"
    assert evidence.source_name == "tmdb"
    assert evidence.aspect == "runtime"
    assert evidence.content == "148"


@pytest.mark.asyncio
async def test_requires_movie_id():
    tools = FakeMovieTools()

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(
        make_request(
            "runtime",
            movie_id=None,
        )
    )

    assert result.completed is False
    assert result.exhausted is True
    assert result.error is not None
    assert "movie_id" in result.error


@pytest.mark.asyncio
async def test_missing_structured_aspect_is_exhausted():
    tools = FakeMovieTools(
        movie={
            "id": 27205,
            "title": "Inception",
        }
    )

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(make_request("runtime"))

    assert result.completed is False
    assert result.exhausted is True
    assert result.error is not None
    assert "runtime" in result.error
    assert result.evidence == []


@pytest.mark.asyncio
async def test_tmdb_failure_returns_error():
    tools = FakeMovieTools(error=RuntimeError("TMDB unavailable"))

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(make_request("runtime"))

    assert result.completed is False
    assert result.exhausted is False
    assert result.error == "TMDB unavailable"
    assert result.evidence == []


@pytest.mark.asyncio
async def test_release_year_alias_is_extracted():
    tools = FakeMovieTools(
        movie={
            "id": 27205,
            "release_date": "2010-07-16",
        }
    )

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(make_request("release_year"))

    assert result.completed is True
    assert result.evidence[0].content == "2010"


@pytest.mark.asyncio
async def test_language_alias_is_extracted():
    tools = FakeMovieTools(
        movie={
            "id": 27205,
            "original_language": "en",
        }
    )

    acquirer = StructuredMovieAcquirer(tools)

    result = await acquirer.acquire(make_request("language"))

    assert result.completed is True
    assert result.evidence[0].content == "en"
