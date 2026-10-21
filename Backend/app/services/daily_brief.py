"""The daily brief: one short digest a day in the notification centre (opt-in).

Built only from the user's own data, so every line is true: what is new for them,
what is due today, what is coming out soon from their watchlist, and one film from
their watchlist for tonight. Nothing is changed and no claim comes from a model.
A day with nothing to say gets no brief. It is off unless the user turns it on
(Settings > Personalisation), and the notifications setting turns it off too.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import Movie, Notification, UserPersonalizationSetting, WatchlistItem

KIND = "daily_brief"
_UPCOMING_DAYS = 7


def _release_date(movie: Movie) -> date | None:
    raw = str((movie.metadata_json or {}).get("release_date") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _film(movie: Movie) -> str:
    released = _release_date(movie)
    year = movie.release_year or (released.year if released else None)
    runtime = movie.runtime or (movie.metadata_json or {}).get("runtime")
    details = ", ".join(str(part) for part in (year, f"{runtime} min" if runtime else None) if part)
    return movie.title + (f" ({details})" if details else "")


def brief_lines(session: Session, user_id: int, now: datetime) -> list[str]:
    """What today's brief says for one user; empty when there is nothing worth a brief."""
    today = now.date()
    start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    lines: list[str] = []
    unread = session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.kind != KIND,
            Notification.read_at.is_(None),
            Notification.dismissed_at.is_(None),
            Notification.due_at <= now,
        )
    )
    if unread:
        lines.append(f"{unread} new notification{'s' if unread != 1 else ''} waiting for you.")
    due_today = session.scalars(
        select(Notification.title).where(
            Notification.user_id == user_id,
            Notification.kind == "reminder",
            Notification.dismissed_at.is_(None),
            Notification.due_at >= now,
            Notification.due_at < start + timedelta(days=1),
        )
    ).all()
    if due_today:
        lines.append("Due later today: " + "; ".join(due_today[:3]) + ".")
    saved = session.execute(
        select(WatchlistItem, Movie)
        .join(Movie, Movie.id == WatchlistItem.movie_id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.created_at.asc())
        .limit(200)
    ).all()
    soon = [
        (released, movie)
        for _item, movie in saved
        if (released := _release_date(movie)) and today < released <= today + timedelta(days=_UPCOMING_DAYS)
    ]
    for released, movie in sorted(soon, key=lambda pair: pair[0])[:2]:
        lines.append(f"Coming out {released.strftime('%A %d %B')}: {_film(movie)}, on your watchlist.")
    out_now = [
        movie for _item, movie in saved if (released := _release_date(movie)) is None or released <= today
    ]
    if out_now:
        # One saved film, a different one each day.
        pick = out_now[today.toordinal() % len(out_now)]
        lines.append(f"From your watchlist for tonight: {_film(pick)}.")
    return lines


def daily_briefs_once(session: Session, *, now: datetime | None = None, limit: int = 500) -> int:
    """Create today's brief for every user who turned it on and has something to hear."""
    now = now or datetime.now(UTC)
    today = now.date()
    start = datetime(today.year, today.month, today.day, tzinfo=UTC)
    users = session.scalars(
        select(UserPersonalizationSetting.user_id)
        .where(
            UserPersonalizationSetting.daily_brief_enabled.is_(True),
            UserPersonalizationSetting.reminders_enabled.is_(True),
        )
        .limit(limit)
    ).all()
    created = 0
    for user_id in users:
        already = session.scalar(
            select(Notification.id).where(
                Notification.user_id == user_id, Notification.kind == KIND, Notification.created_at >= start
            )
        )
        if already is not None:
            continue
        lines = brief_lines(session, user_id, now)
        if not lines:
            continue
        session.add(
            Notification(
                user_id=user_id,
                kind=KIND,
                title="Your daily brief",
                body="\n".join(lines),
                due_at=now,
                created_at=now,
            )
        )
        created += 1
    if created:
        session.commit()
    return created
