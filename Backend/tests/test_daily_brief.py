"""The daily brief (services/daily_brief.py): opt-in, once a day, only true lines from the user's data."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base, Movie, Notification, User, UserPersonalizationSetting, WatchlistItem
from app.services.daily_brief import KIND, brief_lines, daily_briefs_once

NOW = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.add_all([User(username=name, password_hash="x") for name in ("on", "off", "quiet")])
    session.flush()
    session.add_all(
        [
            UserPersonalizationSetting(user_id=1, daily_brief_enabled=True),
            UserPersonalizationSetting(user_id=2, daily_brief_enabled=False),
            UserPersonalizationSetting(user_id=3, daily_brief_enabled=True),
        ]
    )
    arrival = Movie(tmdb_id=329865, title="Arrival", release_year=2016, runtime=116, metadata_json={})
    soon = Movie(tmdb_id=1, title="Next Film", metadata_json={"release_date": "2026-10-20"})
    far = Movie(tmdb_id=2, title="Far Film", metadata_json={"release_date": "2027-05-01"})
    session.add_all([arrival, soon, far])
    session.flush()
    for movie in (arrival, soon, far):
        session.add(WatchlistItem(user_id=1, movie_id=movie.id, created_at=NOW - timedelta(days=30)))
    session.add_all(
        [
            Notification(
                user_id=1, kind="release", title="Old Film is out now", due_at=NOW - timedelta(hours=2)
            ),
            Notification(user_id=1, kind="reminder", title="Watch Arrival", due_at=NOW + timedelta(hours=12)),
            Notification(
                user_id=1, kind="reminder", title="Tomorrow", due_at=NOW + timedelta(days=1, hours=2)
            ),
        ]
    )
    session.commit()
    return session


def test_the_brief_says_only_what_the_users_data_shows():
    db = _db()
    lines = brief_lines(db, 1, NOW)
    assert lines == [
        "1 new notification waiting for you.",
        "Due later today: Watch Arrival.",
        "Coming out Tuesday 20 October: Next Film (2026), on your watchlist.",
        "From your watchlist for tonight: Arrival (2016, 116 min).",
    ]
    assert brief_lines(db, 3, NOW) == []  # nothing to say: no brief


def test_one_brief_a_day_only_for_users_who_turned_it_on():
    db = _db()
    assert daily_briefs_once(db, now=NOW) == 1
    assert daily_briefs_once(db, now=NOW + timedelta(hours=5)) == 0  # never twice a day
    brief = db.scalars(select(Notification).where(Notification.kind == KIND)).one()
    assert (brief.user_id, brief.title) == (1, "Your daily brief")
    assert brief.body.startswith("1 new notification waiting for you.\nDue later today")
    assert daily_briefs_once(db, now=NOW + timedelta(days=1)) == 1  # the next day, a new one
    db.get(UserPersonalizationSetting, 1).reminders_enabled = False
    db.commit()
    assert daily_briefs_once(db, now=NOW + timedelta(days=2)) == 0  # notifications off: no brief


def test_the_worker_creates_briefs_and_survives_a_database_error(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.jobs import process_background

    db = _db()
    monkeypatch.setattr(process_background, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert process_background.daily_briefs_once() == 1

    class Broken:
        def __enter__(self):
            raise OperationalError("select", {}, Exception("down"))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(process_background, "SessionLocal", Broken)
    assert process_background.daily_briefs_once() == 0
