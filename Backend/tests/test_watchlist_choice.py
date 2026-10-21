from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, Movie, User, WatchlistItem


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(User(username="u", password_hash="x"))
        session.flush()
        for tmdb_id, title, runtime, genre in [
            (329865, "Arrival", 116, "Science Fiction"),
            (157336, "Interstellar", 169, "Science Fiction"),
            (1018, "Mulholland Drive", 147, "Mystery"),
        ]:
            movie = Movie(
                tmdb_id=tmdb_id, title=title, runtime=runtime, metadata_json={"genres": [{"name": genre}]}
            )
            session.add(movie)
            session.flush()
            session.add(WatchlistItem(user_id=1, movie_id=movie.id))
        session.commit()
        yield session
    engine.dispose()


async def _pick(db, **constraints):
    """The recommend_movies tool in watchlist mode, as the model calls it."""
    from app.agent import working_state as working
    from app.agent.model_tools import TurnTools

    refs = []
    tools = TurnTools(
        user_message="which one from my watchlist?",
        chat_history=[],
        state=working.empty_state(),
        router=None,
        db=db,
        user_id=1,
        movie_reference_callback=refs.append,
        movie_engine=object(),
    )
    return await tools.recommend_movies({"from_watchlist": True, **constraints}), refs


@pytest.mark.asyncio
async def test_choose_from_watchlist_respects_hard_constraints(db):
    result, refs = await _pick(db, max_runtime_minutes=120)
    assert result["status"] == "found"
    assert [m["tmdb_id"] for m in result["strict_matches"]] == [329865]
    assert refs == [[{"tmdb_id": 329865, "title": "Arrival", "year": None}]]
    # Excluded films keep their reason, so the answer can say why.
    assert any("over your 120-minute limit" in e["reason"] for e in result["excluded"])


@pytest.mark.asyncio
async def test_no_watchlist_film_fits_explains_why(db):
    result, _ = await _pick(db, max_runtime_minutes=90)
    assert result["status"] == "no_watchlist_film_fits" and result["strict_matches"] == []
    reasons = {e["title"]: e["reason"] for e in result["excluded"]}
    assert "116" in reasons["Arrival"]
