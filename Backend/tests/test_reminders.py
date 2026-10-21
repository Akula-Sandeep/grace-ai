"""Reminders and notification centre (UI spec §87, Jarvis spec §34)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.database import Base, Notification, User, UserPersonalizationSetting
from app.services.reminders import ReminderService, parse_when, user_zone

KOLKATA = user_zone("Asia/Kolkata")
# Thursday 2026-10-08 17:00 in Kolkata
NOW = datetime(2026, 10, 8, 11, 30, tzinfo=UTC)


def local(dt):
    return dt.astimezone(KOLKATA).strftime("%a %Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("tomorrow", "Fri 2026-10-09 19:00"),
        ("tomorrow at 9pm", "Fri 2026-10-09 21:00"),
        ("tomorrow morning", "Fri 2026-10-09 09:00"),
        ("tonight", "Thu 2026-10-08 20:30"),
        ("on saturday", "Sat 2026-10-10 19:00"),
        ("this weekend", "Sat 2026-10-10 18:00"),
        ("next thursday", "Thu 2026-10-15 19:00"),
        ("in 2 hours", "Thu 2026-10-08 19:00"),
        ("at 8pm", "Thu 2026-10-08 20:00"),
        ("at 9am", "Fri 2026-10-09 09:00"),  # already past today -> next morning
    ],
)
def test_time_phrases_resolve_in_the_users_time_zone(phrase, expected):
    assert local(parse_when(phrase, now=NOW, zone=KOLKATA)) == expected


@pytest.mark.parametrize("phrase", ["", "sometime", "when I feel like it", "today at 9am"])
def test_unclear_or_past_phrases_are_not_guessed(phrase):
    assert parse_when(phrase, now=NOW, zone=KOLKATA) is None


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/reminders.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    user = User(username="reminder-user", password_hash="x")
    session.add(user)
    session.commit()
    session.info["uid"] = user.id
    yield session
    session.close()
    engine.dispose()


def test_reminder_appears_only_when_due_and_can_be_read_and_dismissed(db):
    uid = db.info["uid"]
    service = ReminderService(db)
    now = datetime.now(UTC)
    due = service.schedule(uid, title="Watch Dune", due_at=now + timedelta(seconds=1), movie_tmdb_id=438631)
    later = service.schedule(uid, title="Watch Arrival", due_at=now + timedelta(days=2))
    assert due.status == later.status == "scheduled"
    assert service.visible(uid, now=now) == []
    assert [n.title for n in service.upcoming(uid, now=now)] == ["Watch Dune", "Watch Arrival"]

    after = now + timedelta(seconds=5)
    assert [n.title for n in service.visible(uid, now=after)] == ["Watch Dune"]
    assert service.unread_count(uid, now=after) == 1
    db.execute(
        update(Notification)
        .values(due_at=now - timedelta(seconds=1))
        .where(Notification.id == due.notification_id)
    )
    db.commit()
    assert service.mark_read(uid) == 1
    assert service.unread_count(uid) == 0
    assert service.dismiss(uid, due.notification_id)
    assert service.visible(uid) == []


def test_reminders_respect_the_users_setting_and_never_schedule_the_past(db):
    uid = db.info["uid"]
    service = ReminderService(db)
    assert service.schedule(uid, title="x", due_at=datetime.now(UTC) - timedelta(minutes=1)).status == "past"
    assert service.schedule(uid, title="x", due_at=None).status == "unparsed"
    db.add(UserPersonalizationSetting(user_id=uid, memory_enabled=True, reminders_enabled=False))
    db.commit()
    assert (
        service.schedule(uid, title="x", due_at=datetime.now(UTC) + timedelta(hours=1)).status == "disabled"
    )
