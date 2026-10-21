"""Reminders and the notification centre (UI spec §86-§87, Jarvis spec §20, §34).

A reminder is a notification with a future ``due_at``; it appears in the
notification centre once due. Natural time phrases ("tomorrow", "Friday at
9pm", "in 2 hours") are parsed deterministically in the user's own time zone,
and a phrase that cannot be understood is rejected rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update

from app.database import Notification, UserPersonalizationSetting

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DEFAULT_EVENING = time(19, 0)
_PARTS_OF_DAY = {
    "morning": time(9, 0),
    "afternoon": time(14, 0),
    "evening": time(19, 0),
    "night": time(20, 30),
}


def user_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _clock(phrase: str) -> time | None:
    match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", phrase)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3) == "pm" else 0)
        minute = int(match.group(2) or 0)
        return time(hour, minute) if hour < 24 and minute < 60 else None
    match = re.search(r"\bat\s+(\d{1,2}):(\d{2})\b", phrase)
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return time(int(match.group(1)), int(match.group(2)))
    for word, value in _PARTS_OF_DAY.items():
        if re.search(rf"\b{word}\b", phrase):
            return value
    return None


def parse_when(phrase: str | None, *, now: datetime, zone: ZoneInfo) -> datetime | None:
    """Return the UTC time a phrase refers to, or ``None`` when it is not understood."""
    text = " ".join(str(phrase or "").casefold().split())
    if not text:
        return None
    local_now = now.astimezone(zone)
    relative = re.search(r"\bin\s+(\d{1,3})\s*(minute|min|hour|hr|day|week)s?\b", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }[unit]
        return (local_now + delta).astimezone(UTC)
    if re.search(r"\blater\b", text) and not re.search(r"\blater (?:this|today)\b", text):
        return (local_now + timedelta(hours=3)).astimezone(UTC)

    clock = _clock(text)
    day = None
    if re.search(r"\btonight\b", text):
        day, clock = local_now.date(), clock or _PARTS_OF_DAY["night"]
    elif re.search(r"\btoday\b|\blater today\b|\bthis (?:evening|afternoon|morning)\b", text):
        day = local_now.date()
    elif re.search(r"\btomorrow\b", text):
        day = local_now.date() + timedelta(days=1)
    elif re.search(r"\bnext week\b", text):
        day = local_now.date() + timedelta(days=7)
    elif re.search(r"\b(?:this )?weekend\b", text):
        ahead = (5 - local_now.weekday()) % 7
        day = local_now.date() + timedelta(days=ahead)
        clock = clock or time(18, 0)
    else:
        for index, name in enumerate(_WEEKDAYS):
            if re.search(rf"\b{name}\b", text):
                ahead = (index - local_now.weekday()) % 7
                if ahead == 0 or re.search(rf"\bnext {name}\b", text):
                    ahead = ahead or 7
                day = local_now.date() + timedelta(days=ahead)
                break
    if day is None and clock is not None:
        day = local_now.date()
    if day is None:
        return None
    candidate = datetime.combine(day, clock or _DEFAULT_EVENING, tzinfo=zone)
    if candidate <= local_now:
        if day == local_now.date() and not re.search(r"\btoday\b|\btonight\b", text):
            candidate += timedelta(days=1)
        else:
            return None  # an explicit time that has already passed is not guessed
    return candidate.astimezone(UTC)


@dataclass
class ReminderResult:
    status: str  # "scheduled" | "disabled" | "unparsed" | "past"
    notification_id: int | None = None
    due_at: datetime | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class ReminderService:
    def __init__(self, db):
        self.db = db

    def reminders_enabled(self, user_id: int) -> bool:
        setting = self.db.scalar(
            select(UserPersonalizationSetting).where(UserPersonalizationSetting.user_id == user_id)
        )
        return setting is None or bool(setting.reminders_enabled)

    def schedule(
        self,
        user_id: int,
        *,
        title: str,
        due_at: datetime | None,
        body: str | None = None,
        movie_tmdb_id: int | None = None,
        now: datetime | None = None,
    ) -> ReminderResult:
        if not self.reminders_enabled(user_id):
            return ReminderResult("disabled")
        if due_at is None:
            return ReminderResult("unparsed")
        due_at = due_at if due_at.tzinfo else due_at.replace(tzinfo=UTC)
        if due_at <= (now or datetime.now(UTC)):
            return ReminderResult("past")
        notification = Notification(
            user_id=user_id,
            kind="reminder",
            title=title[:200],
            body=body,
            movie_tmdb_id=movie_tmdb_id,
            due_at=due_at,
        )
        self.db.add(notification)
        self.db.commit()
        return ReminderResult("scheduled", notification.id, due_at)

    def visible(self, user_id: int, *, now: datetime | None = None, limit: int = 50):
        now = now or datetime.now(UTC)
        return self.db.scalars(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.dismissed_at.is_(None),
                Notification.due_at <= now,
            )
            .order_by(Notification.due_at.desc(), Notification.id.desc())
            .limit(limit)
        ).all()

    def upcoming(self, user_id: int, *, now: datetime | None = None, limit: int = 20):
        now = now or datetime.now(UTC)
        return self.db.scalars(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.dismissed_at.is_(None),
                Notification.due_at > now,
            )
            .order_by(Notification.due_at.asc())
            .limit(limit)
        ).all()

    def unread_count(self, user_id: int, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return int(
            self.db.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id,
                    Notification.dismissed_at.is_(None),
                    Notification.read_at.is_(None),
                    Notification.due_at <= now,
                )
            )
            or 0
        )

    def mark_read(self, user_id: int, notification_id: int | None = None) -> int:
        statement = update(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.due_at <= datetime.now(UTC),
        )
        if notification_id is not None:
            statement = statement.where(Notification.id == notification_id)
        changed = self.db.execute(statement.values(read_at=datetime.now(UTC))).rowcount
        self.db.commit()
        return int(changed or 0)

    def dismiss(self, user_id: int, notification_id: int) -> bool:
        changed = self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.id == notification_id,
                Notification.dismissed_at.is_(None),
            )
            .values(dismissed_at=datetime.now(UTC))
        ).rowcount
        self.db.commit()
        return bool(changed)


def serialize(notification: Notification) -> dict:
    return {
        "id": notification.id,
        "kind": notification.kind,
        "title": notification.title,
        "body": notification.body,
        "movie_tmdb_id": notification.movie_tmdb_id,
        "link_path": notification.link_path,
        "due_at": (_aware(notification.due_at) or notification.created_at).isoformat(),
        "read": notification.read_at is not None,
    }
