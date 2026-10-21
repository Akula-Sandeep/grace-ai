"""Choose from the user's own watchlist ("which one from my watchlist tonight?").

Jarvis spec §22 decision support and UI spec §73 "recommendations from watchlist":
candidates come only from the watchlist, the stated hard constraints are checked
in code, and the model explains the trade-offs between what remains.
"""

from __future__ import annotations

import re
from typing import Any

FROM_WATCHLIST = re.compile(
    r"\b(?:from|on|in|off|out of) my (?:watch ?list|saved (?:movies|films|list))\b|\bwatch ?list pick\b",
    re.IGNORECASE,
)


def _genres(metadata: dict[str, Any] | None) -> list[str]:
    out = []
    for genre in (metadata or {}).get("genres") or []:
        name = genre.get("name") if isinstance(genre, dict) else genre
        if isinstance(name, str):
            out.append(name)
    return out


def watchlist_candidates(session, user_id: int) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.database import Movie, WatchlistItem

    rows = session.execute(
        select(Movie, WatchlistItem.created_at)
        .join(WatchlistItem, WatchlistItem.movie_id == Movie.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.created_at.desc())
        .limit(60)
    ).all()
    out = []
    for movie, saved_at in rows:
        metadata = movie.metadata_json if isinstance(movie.metadata_json, dict) else {}
        out.append(
            {
                "tmdb_id": movie.tmdb_id,
                "title": movie.title,
                "year": movie.release_year,
                "runtime": movie.runtime,
                "genres": _genres(metadata),
                "overview": (movie.overview or metadata.get("overview") or "")[:400],
                "vote_average": metadata.get("vote_average"),
                "saved_at": saved_at.isoformat() if saved_at else None,
            }
        )
    return out


def apply_constraints(
    items: list[dict[str, Any]], classification
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split watchlist items into ones meeting every stated hard constraint and the rest (with reasons)."""
    keep: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    wanted = {g.casefold() for g in (getattr(classification, "genres", None) or [])}
    banned = {g.casefold() for g in (getattr(classification, "exclude_genres", None) or [])}
    for item in items:
        reasons = []
        runtime, year = item.get("runtime"), item.get("year")
        genres = {g.casefold() for g in item.get("genres") or []}
        if classification.max_runtime_minutes and runtime and runtime > classification.max_runtime_minutes:
            reasons.append(f"runs {runtime} min, over your {classification.max_runtime_minutes}-minute limit")
        if classification.min_runtime_minutes and runtime and runtime < classification.min_runtime_minutes:
            reasons.append(f"runs only {runtime} min")
        if classification.min_year and year and year < classification.min_year:
            reasons.append(f"is from {year}")
        if classification.max_year and year and year > classification.max_year:
            reasons.append(f"is from {year}")
        if wanted and genres and not (wanted & genres):
            reasons.append("isn't the genre you asked for")
        if banned & genres:
            reasons.append("is a genre you ruled out")
        (dropped if reasons else keep).append({**item, **({"excluded_because": reasons} if reasons else {})})
    return keep, dropped
