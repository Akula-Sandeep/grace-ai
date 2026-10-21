from datetime import UTC, date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import (
    Base,
    Movie,
    Notification,
    User,
    UserInterfaceSetting,
    UserPersonalizationSetting,
    WatchlistItem,
)
from app.services.proactive import availability_notifications_once, release_notifications_once


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.add_all([User(username="a", password_hash="x"), User(username="b", password_hash="x")])
    session.flush()
    return session


def _save(session, user_id, movie, saved):
    session.add(WatchlistItem(user_id=user_id, movie_id=movie.id, created_at=saved))


def test_release_notification_once_for_films_saved_before_release():
    db = _db()
    upcoming = Movie(tmdb_id=1, title="Future Film", metadata_json={"release_date": "2026-10-05"})
    old = Movie(tmdb_id=2, title="Old Film", metadata_json={"release_date": "2001-01-01"})
    db.add_all([upcoming, old])
    db.flush()
    _save(db, 1, upcoming, datetime(2026, 9, 1, tzinfo=UTC))
    _save(db, 1, old, datetime(2026, 9, 1, tzinfo=UTC))
    _save(db, 2, upcoming, datetime(2026, 9, 1, tzinfo=UTC))
    db.add(UserPersonalizationSetting(user_id=2, reminders_enabled=False))
    db.commit()
    assert release_notifications_once(db, today=date(2026, 10, 9)) == 1
    assert release_notifications_once(db, today=date(2026, 10, 10)) == 0  # never repeats
    note = db.scalars(select(Notification)).one()
    assert (note.user_id, note.kind, note.title) == (1, "release", "Future Film is out now")


def test_availability_notifies_only_on_new_services_after_a_baseline():
    db = _db()
    movie = Movie(tmdb_id=10, title="Saved Film", metadata_json={})
    db.add(movie)
    db.flush()
    _save(db, 1, movie, datetime(2026, 9, 1, tzinfo=UTC))
    db.add(UserInterfaceSetting(user_id=1, region="IN"))
    db.commit()
    providers = {"IN": ["Netflix"]}
    calls = []

    def lookup(tmdb_id, region):
        calls.append((tmdb_id, region))
        return providers[region]

    assert availability_notifications_once(db, lookup) == 0  # baseline only
    assert availability_notifications_once(db, lookup) == 0  # unchanged
    providers["IN"] = ["Netflix", "Prime Video"]
    assert availability_notifications_once(db, lookup) == 1
    assert availability_notifications_once(db, lookup) == 0
    note = db.scalars(select(Notification)).one()
    assert note.title == "Saved Film is now on Prime Video"
    assert set(calls) == {(10, "IN")}


def test_sequel_notice_for_invested_users_only_within_the_window():
    from app.database import Rating, WatchedItem
    from app.services.proactive import sequel_notifications_once

    db = _db()
    first = Movie(tmdb_id=438631, title="Dune", metadata_json={})
    db.add(first)
    db.flush()
    db.add(WatchedItem(user_id=1, movie_id=first.id))
    db.add(Rating(user_id=2, movie_id=first.id, rating=5))  # low rating: not invested
    db.commit()
    parts = {
        "name": "Dune Collection",
        "parts": [
            {"id": 438631, "title": "Dune", "release_date": "2021-09-15"},
            {"id": 693134, "title": "Dune: Part Two", "release_date": "2024-02-27"},
            {"id": 999, "title": "Dune: Part Three", "release_date": "2026-12-18"},
        ],
    }
    calls = []

    def lookup(tmdb_id):
        calls.append(tmdb_id)
        return parts

    assert sequel_notifications_once(db, lookup, today=date(2026, 10, 9)) == 1
    assert sequel_notifications_once(db, lookup, today=date(2026, 10, 10)) == 0
    note = db.scalars(select(Notification)).one()
    assert (note.user_id, note.kind, note.movie_tmdb_id) == (1, "sequel", 999)
    assert note.title == "Dune: Part Three comes out on 2026-12-18" and note.link_path == "/movie/999"


def test_follow_up_once_for_an_undecided_recommendation_discussion():
    from datetime import timedelta

    from app.database import Conversation, Message, MovieReference
    from app.services.proactive import recommendation_follow_ups_once

    db = _db()
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    conversation = Conversation(user_id=1, title="Something tonight", updated_at=now - timedelta(days=3))
    db.add(conversation)
    db.flush()
    message = Message(
        conversation_id=conversation.id, user_id=1, role="assistant", content="Try Arrival or Contact."
    )
    db.add(message)
    db.flush()
    db.add_all(
        [
            MovieReference(
                conversation_id=conversation.id,
                message_id=message.id,
                user_id=1,
                tmdb_id=329865,
                title="Arrival",
                ordinal=0,
            ),
            MovieReference(
                conversation_id=conversation.id,
                message_id=message.id,
                user_id=1,
                tmdb_id=686,
                title="Contact",
                ordinal=1,
            ),
        ]
    )
    db.commit()
    assert recommendation_follow_ups_once(db, now=now) == 1
    assert recommendation_follow_ups_once(db, now=now + timedelta(hours=6)) == 0  # weekly cap
    note = db.scalars(select(Notification)).one()
    assert note.kind == "follow_up" and note.link_path == f"/c/{conversation.id}"
    assert "Arrival, Contact" in note.body


def test_no_follow_up_when_a_pick_was_saved():
    from datetime import timedelta

    from app.database import Conversation, Message, MovieReference
    from app.services.proactive import recommendation_follow_ups_once

    db = _db()
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    conversation = Conversation(user_id=1, title="x", updated_at=now - timedelta(days=3))
    saved = Movie(tmdb_id=329865, title="Arrival", metadata_json={})
    db.add_all([conversation, saved])
    db.flush()
    message = Message(conversation_id=conversation.id, user_id=1, role="assistant", content="...")
    db.add(message)
    db.flush()
    db.add_all(
        [
            MovieReference(
                conversation_id=conversation.id,
                message_id=message.id,
                user_id=1,
                tmdb_id=329865,
                title="Arrival",
                ordinal=0,
            ),
            MovieReference(
                conversation_id=conversation.id,
                message_id=message.id,
                user_id=1,
                tmdb_id=686,
                title="Contact",
                ordinal=1,
            ),
            WatchlistItem(user_id=1, movie_id=saved.id),
        ]
    )
    db.commit()
    assert recommendation_follow_ups_once(db, now=now) == 0
