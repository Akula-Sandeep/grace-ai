import pytest

from app.knowledge import (
    InMemoryMovieKnowledgeStore,
    MovieKnowledge,
)


@pytest.mark.asyncio
async def test_movie_can_be_stored_and_retrieved():
    store = InMemoryMovieKnowledgeStore()

    movie = MovieKnowledge(
        movie_id=27205,
        title="Inception",
        runtime=148,
    )

    await store.save(movie)

    result = await store.get_by_id(27205)

    assert result is not None
    assert result.title == "Inception"
    assert result.runtime == 148


@pytest.mark.asyncio
async def test_movie_can_be_found_by_title():
    store = InMemoryMovieKnowledgeStore()

    movie = MovieKnowledge(
        movie_id=27205,
        title="Inception",
        runtime=148,
    )

    await store.save(movie)

    result = await store.get_by_title("  inception  ")

    assert result is not None
    assert result.movie_id == 27205
    assert result.runtime == 148


@pytest.mark.asyncio
async def test_missing_movie_returns_none():
    store = InMemoryMovieKnowledgeStore()

    result = await store.get_by_title("Definitely Not A Movie")

    assert result is None
