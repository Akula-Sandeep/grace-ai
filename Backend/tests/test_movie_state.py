import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Movie
from app.movie_state import MovieStateService


class FakeTMDB:
    def __init__(self):
        self.calls = 0

    async def get_movie_details(self, tmdb_id):
        self.calls += 1
        return {
            "title": "Arrival",
            "original_title": "Arrival",
            "release_date": "2016-11-11",
            "runtime": 116,
            "overview": "A linguist is recruited by the military.",
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "external_ids": {"imdb_id": "tt2543164"},
        }


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/movie_state_test.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def service(db):
    return MovieStateService(db, FakeTMDB())


@pytest.mark.asyncio
async def test_ensure_movie_creates_and_persists(service, db):
    m = await service.ensure_movie(555)
    assert m.tmdb_id == 555
    assert m.title == "Arrival"
    assert m.release_year == 2016
    assert m.imdb_id == "tt2543164"
    assert db.query(Movie).filter(Movie.tmdb_id == 555).count() == 1


@pytest.mark.asyncio
async def test_ensure_movie_reuses_existing_row_without_calling_tmdb_again(service):
    first = await service.ensure_movie(555)
    second = await service.ensure_movie(555)
    assert first.id == second.id
    assert service.tmdb.calls == 1  # TMDB only hit once; second call reused the DB row


@pytest.mark.asyncio
async def test_ensure_movie_repairs_numeric_title_and_empty_metadata(service, db):
    db.add(Movie(tmdb_id=556, title="556", metadata_json={}))
    db.commit()

    repaired = await service.ensure_movie(556)

    assert repaired.title == "Arrival"
    assert repaired.metadata_json["title"] == "Arrival"
    assert service.tmdb.calls == 1


@pytest.mark.asyncio
async def test_ensure_movie_refuses_to_persist_details_without_title(service, db, monkeypatch):
    from app.services.upstream_errors import UpstreamUnavailable

    async def empty_details(_tmdb_id):
        return {}

    monkeypatch.setattr(service.tmdb, "get_movie_details", empty_details)

    with pytest.raises(UpstreamUnavailable):
        await service.ensure_movie(557)

    assert db.query(Movie).filter(Movie.tmdb_id == 557).count() == 0


@pytest.mark.asyncio
async def test_ensure_movie_recovers_from_concurrent_insert_race(service, db, monkeypatch):
    # Simulate two requests racing to persist the same never-before-seen
    # movie: force the first commit() to raise IntegrityError (as the
    # unique constraint on tmdb_id would for a real concurrent insert),
    # while a "competing" row is already sitting in the DB by the time we
    # roll back and re-query.
    from sqlalchemy.exc import IntegrityError

    real_commit = db.commit
    call_count = {"n": 0}

    def fake_commit():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Insert the "winning" row directly, then fail this commit as
            # if a unique constraint had rejected our own insert.
            db.rollback()
            db.add(Movie(tmdb_id=555, title="Arrival (winner)"))
            real_commit()
            raise IntegrityError("UNIQUE constraint failed", {}, Exception("unique"))
        return real_commit()

    monkeypatch.setattr(db, "commit", fake_commit)

    m = await service.ensure_movie(555)
    assert m.title == "Arrival (winner)"


@pytest.mark.asyncio
async def test_add_watchlist_uses_account_region_for_provider_lookup(db):
    from app.database import User, UserInterfaceSetting

    class RegionTMDB(FakeTMDB):
        def __init__(self):
            super().__init__()
            self.provider_regions = []

        async def get_watch_providers(self, tmdb_id, region):
            self.provider_regions.append((tmdb_id, region))
            return {"flatrate": [{"provider_name": "Example"}], "rent": [], "buy": []}

    tmdb = RegionTMDB()
    service = MovieStateService(db, tmdb)
    user = User(id=11, username="region-user", password_hash="x")
    db.add(user)
    db.add(UserInterfaceSetting(user_id=user.id, region="IN"))
    db.commit()

    result = await service.add_watchlist(user.id, 555)

    assert result["region"] == "IN"
    assert result["streaming_providers"] == ["Example"]
    assert tmdb.provider_regions == [(555, "IN")]


@pytest.mark.asyncio
async def test_add_watchlist_then_remove(service):
    added = await service.add_watchlist(1, 555)
    assert added["status"] == "added"

    added_again = await service.add_watchlist(1, 555)
    assert added_again["status"] == "already_present"

    removed = await service.remove_watchlist(1, 555)
    assert removed["status"] == "removed"

    removed_again = await service.remove_watchlist(1, 999999)
    assert removed_again["status"] == "not_present"


@pytest.mark.asyncio
async def test_state_removals_report_not_present_when_collection_did_not_contain_movie(
    service,
):
    await service.ensure_movie(561)

    assert (await service.remove_watchlist(1, 561))["status"] == "not_present"
    assert (await service.unmark_watched(1, 561))["status"] == "not_present"


@pytest.mark.asyncio
async def test_add_watchlist_recovers_from_duplicate_race(service, db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.database import User, WatchlistItem

    user = User(id=7, username="race-user", password_hash="x")
    db.add(user)
    db.commit()
    await service.ensure_movie(555)
    real_commit = db.commit
    calls = {"count": 0}

    def competing_commit():
        calls["count"] += 1
        if calls["count"] == 1:
            db.rollback()
            movie = db.query(Movie).filter(Movie.tmdb_id == 555).one()
            db.add(WatchlistItem(user_id=user.id, movie_id=movie.id))
            real_commit()
            raise IntegrityError("duplicate", {}, Exception("unique"))
        return real_commit()

    monkeypatch.setattr(db, "commit", competing_commit)
    result = await service.add_watchlist(user.id, 555)

    assert result["ok"] is True
    assert result["status"] == "already_present"
    assert result["movie_title"] == "Arrival"
    assert result["runtime_minutes"] == 116
    assert result["streaming_providers"] == []
    assert db.query(WatchlistItem).filter(WatchlistItem.user_id == user.id).count() == 1


@pytest.mark.asyncio
async def test_mark_watched_returns_typed_status_after_duplicate_race(service, db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.database import User, WatchedItem

    user = User(id=8, username="watched-race-user", password_hash="x")
    db.add(user)
    db.commit()
    movie = await service.ensure_movie(555)
    real_commit = db.commit
    calls = {"count": 0}

    def competing_commit():
        calls["count"] += 1
        if calls["count"] == 1:
            db.rollback()
            db.add(WatchedItem(user_id=user.id, movie_id=movie.id))
            real_commit()
            raise IntegrityError("duplicate", {}, Exception("unique"))
        return real_commit()

    monkeypatch.setattr(db, "commit", competing_commit)
    result = await service.mark_watched(user.id, 555)

    assert result["status"] == "already_present"
    assert result["movie"] == "Arrival"
    assert db.query(WatchedItem).filter(WatchedItem.user_id == user.id).count() == 1


@pytest.mark.asyncio
async def test_move_to_watched_is_atomic_and_converges_to_watched_only(service, db):
    from app.database import User, WatchedItem, WatchlistItem

    user = User(id=9, username="move-user", password_hash="x")
    db.add(user)
    db.commit()
    movie = await service.ensure_movie(555)
    db.add(WatchlistItem(user_id=user.id, movie_id=movie.id))
    db.commit()

    result = await service.move_to_watched(user.id, 555)

    assert result["status"] == "moved"
    assert (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.movie_id == movie.id)
        .count()
        == 0
    )
    assert (
        db.query(WatchedItem).filter(WatchedItem.user_id == user.id, WatchedItem.movie_id == movie.id).count()
        == 1
    )

    repeated = await service.move_to_watched(user.id, 555)
    assert repeated["status"] == "already_present"
    assert (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.movie_id == movie.id)
        .count()
        == 0
    )
    assert (
        db.query(WatchedItem).filter(WatchedItem.user_id == user.id, WatchedItem.movie_id == movie.id).count()
        == 1
    )


@pytest.mark.asyncio
async def test_mark_and_unmark_watched(service):
    marked = await service.mark_watched(1, 555)
    assert marked["status"] == "added"
    marked_again = await service.mark_watched(1, 555)
    assert marked_again["status"] == "already_present"
    unmarked = await service.unmark_watched(1, 555)
    assert unmarked["status"] == "removed"
    assert (await service.unmark_watched(1, 555))["status"] == "not_present"


@pytest.mark.asyncio
async def test_rate_rejects_out_of_range_values(service):
    with pytest.raises(ValueError):
        await service.rate(1, 555, 11)
    with pytest.raises(ValueError):
        await service.rate(1, 555, -1)


@pytest.mark.asyncio
async def test_rate_creates_then_updates(service, db):
    from app.database import Rating

    await service.rate(1, 555, 7.5)
    result = await service.rate(1, 555, 9.0)
    assert result["rating"] == 9.0
    # Updating an existing rating should not create a second row.
    assert db.query(Rating).filter(Rating.user_id == 1).count() == 1


@pytest.mark.asyncio
async def test_feedback_with_movie_links_it_and_updates_memory(service):
    result = await service.feedback(1, 555, "like", "loved the sound design")
    assert result["movie"] == "Arrival"
    assert result["tmdb_id"] == 555


@pytest.mark.asyncio
async def test_feedback_without_movie_id_still_records(service):
    result = await service.feedback(1, None, "general", "I prefer subtitles over dubbing")
    assert result["movie"] is None
    assert result["tmdb_id"] is None
