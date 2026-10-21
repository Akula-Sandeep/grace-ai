"""Sparse, user-controlled proactive notifications (product spec §29, UI spec §86-§87).

Two signals only, both about films the user saved themselves:

* release   - a watchlist film that was unreleased when saved is now out;
* availability - a watchlist film became available on a new streaming service
  in the user's region (the first check only records a baseline).

Both respect the user's reminders/notifications setting and never repeat.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import Movie, Notification, UserInterfaceSetting, WatchlistItem
from app.memory import ANSWER_CARD
from app.services.reminders import ReminderService


def _release_date(movie: Movie) -> date | None:
    raw = str((movie.metadata_json or {}).get("release_date") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _already_sent(session, user_id: int, kind: str, tmdb_id: int, title: str) -> bool:
    return (
        session.scalar(
            select(Notification.id).where(
                Notification.user_id == user_id,
                Notification.kind == kind,
                Notification.movie_tmdb_id == tmdb_id,
                Notification.title == title,
            )
        )
        is not None
    )


def release_notifications_once(session, *, today: date | None = None, limit: int = 500) -> int:
    today = today or datetime.now(UTC).date()
    service = ReminderService(session)
    created = 0
    rows = session.execute(
        select(WatchlistItem, Movie).join(Movie, Movie.id == WatchlistItem.movie_id).limit(limit)
    ).all()
    for item, movie in rows:
        released = _release_date(movie)
        saved_on = (item.created_at or datetime.now(UTC)).date()
        if not released or not item.user_id or not (saved_on < released <= today):
            continue
        title = f"{movie.title} is out now"
        if not service.reminders_enabled(item.user_id) or _already_sent(
            session, item.user_id, "release", movie.tmdb_id, title
        ):
            continue
        session.add(
            Notification(
                user_id=item.user_id,
                kind="release",
                title=title[:200],
                body=f"You saved {movie.title} before it came out. It was released on {released.isoformat()}.",
                movie_tmdb_id=movie.tmdb_id,
                due_at=datetime.now(UTC),
            )
        )
        created += 1
    if created:
        session.commit()
    return created


def availability_notifications_once(
    session,
    lookup_providers: Callable[[int, str], list[str]],
    *,
    limit: int = 50,
) -> int:
    """Notify when a saved film gains a subscription service in the user's region."""
    service = ReminderService(session)
    regions = {
        row.user_id: (row.region or settings.default_region).upper()
        for row in session.scalars(select(UserInterfaceSetting))
    }
    rows = session.execute(
        select(WatchlistItem, Movie).join(Movie, Movie.id == WatchlistItem.movie_id).limit(limit * 4)
    ).all()
    by_movie: dict[int, list[tuple[WatchlistItem, Movie]]] = {}
    for item, movie in rows:
        if item.user_id:
            by_movie.setdefault(movie.id, []).append((item, movie))
    created = 0
    for entries in list(by_movie.values())[:limit]:
        movie = entries[0][1]
        metadata: dict[str, Any] = dict(movie.metadata_json or {})
        snapshot: dict[str, list[str]] = dict(metadata.get("provider_snapshot") or {})
        for region in sorted(
            {regions.get(item.user_id, settings.default_region.upper()) for item, _ in entries}
        ):
            current = sorted(set(lookup_providers(movie.tmdb_id, region)))
            previous = snapshot.get(region)
            snapshot[region] = current
            if previous is None:
                continue  # first check records a baseline, it does not notify
            added = [name for name in current if name not in previous]
            if not added:
                continue
            for item, _ in entries:
                if regions.get(item.user_id, settings.default_region.upper()) != region:
                    continue
                title = f"{movie.title} is now on {', '.join(added)}"
                if not service.reminders_enabled(item.user_id) or _already_sent(
                    session, item.user_id, "availability", movie.tmdb_id, title
                ):
                    continue
                session.add(
                    Notification(
                        user_id=item.user_id,
                        kind="availability",
                        title=title[:200],
                        body=f"A film on your watchlist became available to stream in {region}.",
                        movie_tmdb_id=movie.tmdb_id,
                        due_at=datetime.now(UTC),
                    )
                )
                created += 1
        metadata["provider_snapshot"] = snapshot
        movie.metadata_json = metadata
        flag_modified(movie, "metadata_json")
    session.commit()
    return created


# --- Sequel / new-release awareness ---------------------------------------------------

SEQUEL_WINDOW_PAST_DAYS = 30
SEQUEL_WINDOW_FUTURE_DAYS = 120


def sequel_notifications_once(
    session,
    lookup_collection: Callable[[int], dict[str, Any] | None],
    *,
    today: date | None = None,
    limit: int = 40,
) -> int:
    """Notify about a new or upcoming part of a collection the user is invested in.

    "Invested" means the user watched an earlier part or rated it 7/10 or higher.
    Only parts released in the last 30 days or due within 120 days count, and
    never ones already on the user's lists. ``lookup_collection(tmdb_id)`` returns
    ``{"name": ..., "parts": [{"id", "title", "release_date"}]}`` or None.
    """
    from app.database import Rating, WatchedItem

    today = today or datetime.now(UTC).date()
    service = ReminderService(session)
    interest: dict[int, set[int]] = {}
    for user_id, movie_id in session.execute(select(WatchedItem.user_id, WatchedItem.movie_id)).all():
        interest.setdefault(movie_id, set()).add(user_id)
    for user_id, movie_id in session.execute(
        select(Rating.user_id, Rating.movie_id).where(Rating.rating >= 7)
    ).all():
        interest.setdefault(movie_id, set()).add(user_id)
    if not interest:
        return 0
    movies = {m.id: m for m in session.scalars(select(Movie).where(Movie.id.in_(list(interest)[:limit])))}
    owned: dict[int, set[int]] = {}
    for user_id, tmdb_id in session.execute(
        select(WatchlistItem.user_id, Movie.tmdb_id).join(Movie, Movie.id == WatchlistItem.movie_id)
    ).all():
        owned.setdefault(user_id, set()).add(tmdb_id)
    for user_id, tmdb_id in session.execute(
        select(WatchedItem.user_id, Movie.tmdb_id).join(Movie, Movie.id == WatchedItem.movie_id)
    ).all():
        owned.setdefault(user_id, set()).add(tmdb_id)
    created = 0
    seen_collections: dict[Any, dict[str, Any] | None] = {}
    for movie_id, users in interest.items():
        movie = movies.get(movie_id)
        if movie is None:
            continue
        collection = seen_collections.get(movie.tmdb_id)
        if movie.tmdb_id not in seen_collections:
            collection = lookup_collection(movie.tmdb_id)
            seen_collections[movie.tmdb_id] = collection
        for part in (collection or {}).get("parts") or []:
            try:
                released = date.fromisoformat(str(part.get("release_date") or "")[:10])
            except ValueError:
                continue
            delta = (released - today).days
            if (
                not (-SEQUEL_WINDOW_PAST_DAYS <= delta <= SEQUEL_WINDOW_FUTURE_DAYS)
                or part.get("id") == movie.tmdb_id
            ):
                continue
            when = "is out now" if delta <= 0 else f"comes out on {released.isoformat()}"
            title = f"{part.get('title')} {when}"[:200]
            for user_id in users:
                if part.get("id") in owned.get(user_id, set()) or not service.reminders_enabled(user_id):
                    continue
                if _already_sent(session, user_id, "sequel", int(part["id"]), title):
                    continue
                session.add(
                    Notification(
                        user_id=user_id,
                        kind="sequel",
                        title=title,
                        body=f"A new part of {(collection or {}).get('name') or 'a series'} you've watched ({movie.title}).",
                        movie_tmdb_id=int(part["id"]),
                        link_path=f"/movie/{int(part['id'])}",
                        due_at=datetime.now(UTC),
                    )
                )
                created += 1
    if created:
        session.commit()
    return created


# --- Unresolved recommendation follow-up ------------------------------------------------

FOLLOW_UP_AFTER_DAYS = 2
FOLLOW_UP_STALE_DAYS = 7
FOLLOW_UP_MIN_GAP_DAYS = 7


def recommendation_follow_ups_once(session, *, now: datetime | None = None) -> int:
    """One gentle nudge when a recommendation discussion ended undecided.

    A conversation qualifies when its latest assistant answer suggested at least
    two films, it went quiet 2-7 days ago, and none of those films was saved or
    marked watched since. At most one follow-up per user per week.
    """
    from datetime import timedelta

    from app.database import Conversation, MovieReference, WatchedItem

    now = now or datetime.now(UTC)
    service = ReminderService(session)
    window_start, window_end = now - timedelta(days=FOLLOW_UP_STALE_DAYS), now - timedelta(
        days=FOLLOW_UP_AFTER_DAYS
    )
    conversations = session.scalars(
        select(Conversation).where(
            Conversation.is_deleted.is_(False),
            Conversation.is_archived.is_(False),
            Conversation.updated_at >= window_start,
            Conversation.updated_at <= window_end,
        )
    ).all()
    created = 0
    nudged_users: set[int] = set()
    for conversation in conversations:
        user_id = conversation.user_id
        if user_id in nudged_users or not service.reminders_enabled(user_id):
            continue
        recent = session.scalar(
            select(Notification.id).where(
                Notification.user_id == user_id,
                Notification.kind == "follow_up",
                Notification.created_at >= now - timedelta(days=FOLLOW_UP_MIN_GAP_DAYS),
            )
        )
        if recent is not None:
            continue
        refs = session.execute(
            select(MovieReference.message_id, MovieReference.tmdb_id, MovieReference.title)
            .where(MovieReference.conversation_id == conversation.id, MovieReference.source != ANSWER_CARD)
            .order_by(MovieReference.message_id.desc(), MovieReference.ordinal)
        ).all()
        if not refs:
            continue
        last_message = refs[0][0]
        picks = [(tmdb_id, title) for message_id, tmdb_id, title in refs if message_id == last_message]
        if len(picks) < 2:
            continue
        pick_ids = [tmdb_id for tmdb_id, _ in picks]
        acted = session.scalar(
            select(Movie.id)
            .where(Movie.tmdb_id.in_(pick_ids))
            .where(
                Movie.id.in_(select(WatchlistItem.movie_id).where(WatchlistItem.user_id == user_id))
                | Movie.id.in_(select(WatchedItem.movie_id).where(WatchedItem.user_id == user_id))
            )
            .limit(1)
        )
        if acted is not None:
            continue
        titles = ", ".join(title for _, title in picks[:3])
        session.add(
            Notification(
                user_id=user_id,
                kind="follow_up",
                title="Still deciding what to watch?",
                body=f"Last time I suggested {titles}. Want to pick one, or try something different?",
                link_path=f"/c/{conversation.id}",
                due_at=now,
            )
        )
        nudged_users.add(user_id)
        created += 1
    if created:
        session.commit()
    return created
