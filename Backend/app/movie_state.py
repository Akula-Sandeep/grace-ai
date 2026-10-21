from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import (
    Feedback,
    FeedbackInferenceJob,
    Movie,
    Rating,
    User,
    UserInterfaceSetting,
    WatchedItem,
    WatchlistItem,
    run_db,
)
from app.observability import logger
from app.services.upstream_errors import UpstreamUnavailable


class MovieStateResult(TypedDict):
    ok: bool
    status: Literal["added", "already_present", "removed", "not_present", "moved"]
    tmdb_id: int
    movie: NotRequired[str]
    movie_title: NotRequired[str]
    already_present: NotRequired[bool]
    already_watched: NotRequired[bool]
    removed: NotRequired[bool]
    runtime_minutes: NotRequired[int | None]
    streaming_providers: NotRequired[list[str]]
    watchlist_state: NotRequired[list[dict[str, object]]]
    region: NotRequired[str]


class MovieStateService:
    """Movie-state service with an explicit synchronous DB boundary.

    Tests and synchronous agent code may still pass a live SQLAlchemy session.
    Async API routes pass ``db_factory`` so every ORM unit executes on a worker
    thread and no session crosses an await boundary.
    """

    def __init__(self, db, tmdb, db_factory=None):
        self.db = db
        self.tmdb = tmdb
        self.db_factory = db_factory

    async def _db_call(self, fn):
        if self.db is not None:
            return fn(self.db)
        return await run_db(fn, session_factory=self.db_factory)

    def _bump_taste_profile_version(self, db, user_id: int) -> None:
        db.execute(
            update(User)
            .where(User.id == user_id)
            .values(taste_profile_version=User.taste_profile_version + 1)
        )

    async def ensure_movie(self, tmdb_id):
        local = await self._db_call(lambda db: db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id)))
        if (
            local
            and isinstance(local.title, str)
            and local.title.strip()
            and local.title.strip() != str(tmdb_id)
            and isinstance(local.metadata_json, dict)
            and local.metadata_json
        ):
            return local

        details = await self.tmdb.get_movie_details(tmdb_id)
        if not isinstance(details, dict):
            raise UpstreamUnavailable("TMDB returned invalid movie details.")
        title = details.get("title") or details.get("original_title")
        if not isinstance(title, str) or not title.strip() or title.strip() == str(tmdb_id):
            raise UpstreamUnavailable("TMDB did not provide a valid movie title.")
        title = title.strip()
        date = details.get("release_date")
        release_year = (
            int(date[:4]) if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit() else None
        )

        def persist(db):
            row = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
            if row is None:
                row = Movie(tmdb_id=tmdb_id)
                db.add(row)
            row.title = title
            row.original_title = details.get("original_title")
            row.imdb_id = (details.get("external_ids") or {}).get("imdb_id")
            row.release_year = release_year
            row.runtime = details.get("runtime")
            row.overview = details.get("overview")
            row.poster_path = details.get("poster_path")
            row.backdrop_path = details.get("backdrop_path")
            row.metadata_json = details
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
                if row is None:
                    raise
            return row

        return await self._db_call(persist)

    async def add_watchlist(self, uid, tmdb_id, note=None, region=None) -> MovieStateResult:
        movie = await self.ensure_movie(tmdb_id)
        account_region = await self._db_call(
            lambda db: db.scalar(
                select(UserInterfaceSetting.region).where(UserInterfaceSetting.user_id == uid)
            )
        )
        resolved_region = (region or account_region or settings.default_region).upper()

        def mutate(db):
            existing = db.scalar(
                select(WatchlistItem).where(
                    WatchlistItem.user_id == uid,
                    WatchlistItem.movie_id == movie.id,
                )
            )
            if existing is not None:
                if note:
                    existing.note = note
                db.commit()
                return False
            db.add(WatchlistItem(user_id=uid, movie_id=movie.id, note=note))
            self._bump_taste_profile_version(db, uid)
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

        added = await self._db_call(mutate)

        runtime = movie.runtime
        if runtime is None:
            try:
                details = await self.tmdb.get_movie_details(tmdb_id)
                runtime = details.get("runtime")

                def update_runtime(db):
                    row = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
                    if row is None:
                        return
                    row.runtime = runtime
                    row.metadata_json = details
                    db.commit()

                await self._db_call(update_runtime)
            except Exception as exc:
                logger.warning("watchlist_runtime_lookup_failed tmdb_id=%s error=%r", tmdb_id, exc)

        providers: list[str] = []
        provider_lookup = getattr(self.tmdb, "get_watch_providers", None)
        if provider_lookup is not None:
            try:
                provider_data = await provider_lookup(tmdb_id, resolved_region)
                if not isinstance(provider_data, dict):
                    raise TypeError("TMDB watch-provider response was not an object.")
                providers = list(
                    dict.fromkeys(
                        provider.get("provider_name")
                        for category in ("flatrate", "rent", "buy")
                        for provider in provider_data.get(category, [])
                        if isinstance(provider, dict) and provider.get("provider_name")
                    )
                )
            except Exception as exc:
                logger.warning("watchlist_provider_lookup_failed tmdb_id=%s error=%r", tmdb_id, exc)

        return {
            "ok": True,
            "status": "added" if added else "already_present",
            "movie": movie.title,
            "movie_title": movie.title,
            "tmdb_id": movie.tmdb_id,
            "already_present": not added,
            "runtime_minutes": runtime,
            "streaming_providers": providers,
            "region": resolved_region,
        }

    async def remove_watchlist(self, uid, tmdb_id) -> MovieStateResult:
        movie = await self._db_call(lambda db: db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id)))
        if movie is None:
            return {
                "ok": True,
                "status": "not_present",
                "tmdb_id": tmdb_id,
                "removed": False,
            }

        def mutate(db):
            result = db.execute(
                delete(WatchlistItem).where(WatchlistItem.user_id == uid, WatchlistItem.movie_id == movie.id)
            )
            removed = result.rowcount == 1
            if removed:
                self._bump_taste_profile_version(db, uid)
            db.commit()
            return removed

        removed = await self._db_call(mutate)
        return {
            "ok": True,
            "status": "removed" if removed else "not_present",
            "movie": movie.title,
            "tmdb_id": movie.tmdb_id,
            "removed": removed,
        }

    async def mark_watched(self, uid, tmdb_id) -> MovieStateResult:
        movie = await self.ensure_movie(tmdb_id)

        def mutate(db):
            existing = db.scalar(
                select(WatchedItem).where(WatchedItem.user_id == uid, WatchedItem.movie_id == movie.id)
            )
            if existing is not None:
                return False
            db.add(WatchedItem(user_id=uid, movie_id=movie.id))
            self._bump_taste_profile_version(db, uid)
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

        added = await self._db_call(mutate)
        return {
            "ok": True,
            "status": "added" if added else "already_present",
            "movie": movie.title,
            "tmdb_id": movie.tmdb_id,
            "already_watched": not added,
        }

    async def move_to_watched(self, uid, tmdb_id) -> MovieStateResult:
        """Atomically remove watchlist membership and add watched membership."""
        movie = await self.ensure_movie(tmdb_id)

        def mutate(db):
            watched = db.scalar(
                select(WatchedItem).where(WatchedItem.user_id == uid, WatchedItem.movie_id == movie.id)
            )
            watchlist = db.scalar(
                select(WatchlistItem).where(WatchlistItem.user_id == uid, WatchlistItem.movie_id == movie.id)
            )
            changed = False
            if watched is None:
                db.add(WatchedItem(user_id=uid, movie_id=movie.id))
                changed = True
            if watchlist is not None:
                db.delete(watchlist)
                changed = True
            if changed:
                self._bump_taste_profile_version(db, uid)
            try:
                db.commit()
                return changed
            except IntegrityError:
                db.rollback()
                # Another transaction won the watched insert race. Reconcile the
                # authoritative state in a fresh transaction so the move command
                # still converges to watched-only rather than leaving both rows.
                final_watched = db.scalar(
                    select(WatchedItem).where(
                        WatchedItem.user_id == uid,
                        WatchedItem.movie_id == movie.id,
                    )
                )
                final_watchlist = db.scalar(
                    select(WatchlistItem).where(
                        WatchlistItem.user_id == uid,
                        WatchlistItem.movie_id == movie.id,
                    )
                )
                cleanup_changed = False
                if final_watched is None:
                    db.add(WatchedItem(user_id=uid, movie_id=movie.id))
                    cleanup_changed = True
                if final_watchlist is not None:
                    db.delete(final_watchlist)
                    cleanup_changed = True
                if cleanup_changed:
                    self._bump_taste_profile_version(db, uid)
                db.commit()
                return cleanup_changed

        changed = await self._db_call(mutate)
        return {
            "ok": True,
            "status": "moved" if changed else "already_present",
            "movie": movie.title,
            "movie_title": movie.title,
            "tmdb_id": movie.tmdb_id,
            "already_watched": not changed,
        }

    def list_watchlist(self, uid, limit: int | None = None):
        query = (
            select(
                Movie.title,
                Movie.tmdb_id,
                Movie.release_year,
                Movie.runtime,
                WatchlistItem.created_at,
            )
            .join(WatchlistItem, WatchlistItem.movie_id == Movie.id)
            .where(WatchlistItem.user_id == uid)
            .order_by(WatchlistItem.created_at.desc(), Movie.id.desc())
        )
        if limit is not None:
            query = query.limit(max(1, min(int(limit), 100)))
        rows = self.db.execute(query).all()
        return [
            {
                "title": title,
                "tmdb_id": tmdb_id,
                "release_year": year,
                "runtime": runtime,
                "created_at": created_at,
            }
            for title, tmdb_id, year, runtime, created_at in rows
        ]

    async def _list_watchlist_async(self, uid, limit: int = 100):
        return await self._db_call(lambda db: self.__class__(db, None).list_watchlist(uid, limit=limit))

    def adopt_guest_state(self, guest_items, user_id: int) -> dict[str, int]:
        """Attach nullable guest movie-state rows to an authenticated user."""
        migrated = {"watchlist": 0, "watched": 0}
        for item in guest_items if isinstance(guest_items, list) else []:
            if not isinstance(item, dict):
                continue
            tmdb_id = item.get("tmdb_id")
            if not isinstance(tmdb_id, int):
                continue
            movie = self.db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
            if movie is None:
                continue
            for model, key in ((WatchlistItem, "watchlist"), (WatchedItem, "watched")):
                row = self.db.scalar(select(model).where(model.user_id.is_(None), model.movie_id == movie.id))
                if row is not None:
                    row.user_id = user_id
                    migrated[key] += 1
        if migrated["watchlist"] or migrated["watched"]:
            self._bump_taste_profile_version(self.db, user_id)
        self.db.commit()
        return migrated

    def list_watched(self, uid, limit: int | None = None):
        query = (
            select(
                Movie.title,
                Movie.tmdb_id,
                Movie.release_year,
                Movie.runtime,
                WatchedItem.watched_at,
            )
            .join(WatchedItem, WatchedItem.movie_id == Movie.id)
            .where(WatchedItem.user_id == uid)
            .order_by(WatchedItem.watched_at.desc(), Movie.id.desc())
        )
        if limit is not None:
            query = query.limit(max(1, min(int(limit), 100)))
        rows = self.db.execute(query).all()
        return [
            {
                "title": title,
                "tmdb_id": tmdb_id,
                "release_year": year,
                "runtime": runtime,
                "watched_at": watched_at,
            }
            for title, tmdb_id, year, runtime, watched_at in rows
        ]

    async def unmark_watched(self, uid, tmdb_id) -> MovieStateResult:
        movie = await self._db_call(lambda db: db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id)))
        if movie is None:
            return {
                "ok": True,
                "status": "not_present",
                "tmdb_id": tmdb_id,
                "removed": False,
            }

        def mutate(db):
            result = db.execute(
                delete(WatchedItem).where(WatchedItem.user_id == uid, WatchedItem.movie_id == movie.id)
            )
            removed = result.rowcount == 1
            if removed:
                self._bump_taste_profile_version(db, uid)
            db.commit()
            return removed

        removed = await self._db_call(mutate)
        return {
            "ok": True,
            "status": "removed" if removed else "not_present",
            "movie": movie.title,
            "tmdb_id": movie.tmdb_id,
            "removed": removed,
        }

    async def rate(self, uid, tmdb_id, rating):
        if not 0 <= float(rating) <= 10:
            raise ValueError("Rating must be between 0 and 10.")
        movie = await self.ensure_movie(tmdb_id)
        value = float(rating)

        def mutate(db):
            row = db.scalar(select(Rating).where(Rating.user_id == uid, Rating.movie_id == movie.id))
            if row is None:
                db.add(Rating(user_id=uid, movie_id=movie.id, rating=value))
            else:
                row.rating = value
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.scalar(select(Rating).where(Rating.user_id == uid, Rating.movie_id == movie.id))
                if row is None:
                    raise
                row.rating = value
                db.commit()

        await self._db_call(mutate)
        return {"ok": True, "movie": movie.title, "rating": value}

    async def feedback(self, uid, tmdb_id, kind, text, *, idempotency_key: str | None = None):
        movie = await self.ensure_movie(tmdb_id) if tmdb_id else None
        mid = movie.id if movie else None

        def persist(db):

            if idempotency_key:
                existing = db.scalar(
                    select(Feedback).where(
                        Feedback.user_id == uid,
                        Feedback.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing, False
            feedback = Feedback(
                user_id=uid,
                movie_id=mid,
                feedback_type=kind,
                text=text,
                idempotency_key=idempotency_key,
            )
            db.add(feedback)
            db.flush()
            db.add(FeedbackInferenceJob(feedback_id=feedback.id, user_id=uid))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(Feedback).where(
                        Feedback.user_id == uid,
                        Feedback.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                return existing, False
            return feedback, True

        feedback_row, created = await self._db_call(persist)
        return {
            "ok": True,
            "text": feedback_row.text,
            "movie": movie.title if movie else None,
            "tmdb_id": tmdb_id,
            "inference_status": "queued" if created else "already_queued",
            "feedback_id": feedback_row.id,
        }
