"""Per-conversation working state (product spec §3.5, Jarvis spec §10-§11).

A small, bounded record of what the current conversation is about: recent
movies, people, places and characters, the active comparison, and a question
Grace is waiting on. It is not durable memory; it expires with the
conversation's activity and only resolves clear follow-ups.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from app.observability import logger
from app.shared_state import shared_state

TTL_SECONDS = 6 * 60 * 60
_LIMIT = 6

_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}


def empty_state() -> dict[str, Any]:
    return {
        "movies": [],
        "people": [],
        "places": [],
        "characters": [],
        "comparison": None,
        "pending": None,
        # Films shown in the previous answer, in the order shown: what "it",
        # "the second one" and "both" refer to (Jarvis spec §11, §21).
        "last_answer_movies": [],
        "turn_movies": [],
        "focus": None,
        "handled": [],
        # The previous reply presented its films as a list (see order_by_answer).
        "turn_listed": False,
    }


def normalize(value: Any) -> dict[str, Any]:
    """A saved state (from a message or the cache), completed with any new fields."""
    return {**empty_state(), **value} if isinstance(value, dict) else empty_state()


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """The part of the state worth saving with the reply: bounded and JSON-safe."""
    keep = (
        "movies",
        "people",
        "places",
        "characters",
        "comparison",
        "pending",
        "last_answer_movies",
        "turn_movies",
        "turn_listed",
        "focus",
        "handled",
        "active_request",
        "last_action",
        "recent_action",
    )
    out: dict[str, Any] = {}
    for key in keep:
        value = state.get(key)
        if isinstance(value, list):
            value = value[-12:]
        out[key] = value
    out["version"] = 1
    return json.loads(json.dumps(out, default=str))


def start_turn(state: dict[str, Any]) -> None:
    """Roll the previous turn's films into ``last_answer_movies``.

    A turn that showed no films (a greeting, a maths question) keeps the
    earlier ones, so "add it" still means the film being discussed.
    """
    # The list change the previous reply completed (only that reply's; see confirmations.py).
    state["previous_action"] = state.pop("last_action", None)
    turn = state.get("turn_movies") or []
    shown = state.get("last_answer_movies") or []
    shown_titles = {m.get("title") for m in shown}
    listed = bool(state.get("turn_listed"))
    state["turn_listed"] = False
    if turn and shown and not listed and all(m.get("title") in shown_titles for m in turn):
        # The last turn acted on films from the list ("add the second one"):
        # keep the list, so "the others" / "the third one" still work, and
        # remember which one was just handled ("remind me about it").
        state["focus"] = turn[-1]
        handled = [*(state.get("handled") or []), *(m.get("title") for m in turn)]
        state["handled"] = list(dict.fromkeys(handled))[-8:]
    elif turn:
        state["last_answer_movies"] = turn[:8]
        state["focus"] = turn[0] if len(turn) == 1 else None
        state["handled"] = []
    state["turn_movies"] = []


async def load(key: str | None) -> dict[str, Any]:
    if not key:
        return empty_state()
    try:
        value = await shared_state.cache_get(f"working-state:{key}", TTL_SECONDS)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("working_state_load_failed error=%r", exc)
        return empty_state()
    return {**empty_state(), **value} if isinstance(value, dict) else empty_state()


async def save(key: str | None, state: dict[str, Any]) -> None:
    if not key:
        return
    state["updated_at"] = datetime.now(UTC).isoformat()
    try:
        await shared_state.cache_set(f"working-state:{key}", state, 5000, TTL_SECONDS)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("working_state_save_failed error=%r", exc)


def _remember(items: list, value: Any, key: str | None = None) -> list:
    identity = (lambda item: item.get(key)) if key else (lambda item: item)
    kept = [item for item in items if identity(item) != identity(value)]
    return [*kept, value][-_LIMIT:]


# Intents that act on films already shown; their replies name those films but are not a new list.
ACTION_INTENTS = frozenset(
    {
        "add_to_watchlist",
        "remove_from_watchlist",
        "mark_as_watched",
        "unmark_watched",
        "set_reminder",
        "rate_movie",
    }
)


# A list or table row: "- …", "* …", "1. …", "| …" (not a line that starts in bold).
def order_by_answer(state: dict[str, Any], answer: str, *, acted: bool = False) -> None:
    """Order this turn's films (from tool results) the way the reply presented them.

    The state block then lists them in the order the user read them, so "the second
    one" means the second film they saw. Films only ever come from tool results; the
    model resolves anything else from the conversation itself.
    """
    turn = state.get("turn_movies") or []
    state["turn_listed"] = False
    if not answer or len(turn) < 2:
        return
    text = answer.casefold()

    def position(movie: dict[str, Any]) -> int:
        index = text.find(str(movie.get("title") or "").casefold())
        return index if index >= 0 else len(text) + 1

    mentioned = [movie for movie in turn if position(movie) <= len(text)]
    if not acted and len(mentioned) >= 2:
        # The reply presented these films: they are the list "the second one" refers to.
        state["turn_movies"] = sorted(mentioned, key=position)
        state["turn_listed"] = True
        return
    state["turn_movies"] = sorted(turn, key=position)


def record_references(state: dict[str, Any], references: list[dict[str, Any]]) -> None:
    for reference in references:
        if reference.get("kind") == "person" and reference.get("title"):
            state["people"] = _remember(
                state["people"], {"name": reference["title"], "person_id": reference.get("person_id")}, "name"
            )
        elif reference.get("title"):
            entry = {"title": reference["title"], "tmdb_id": reference.get("tmdb_id")}
            year = reference.get("year") or str(reference.get("release_date") or "")[:4]
            if str(year).isdigit():
                entry["year"] = int(year)
            state["movies"] = _remember(state["movies"], entry, "title")
            turn = state.setdefault("turn_movies", [])
            if not any(item.get("title") == reference["title"] for item in turn) and len(turn) < 12:
                turn.append(dict(entry))


def record_result(state: dict[str, Any], result: Any) -> None:
    """Pick entities out of a structured lookup result."""
    if not isinstance(result, dict):
        return
    if result.get("person"):
        state["people"] = _remember(
            state["people"], {"name": result["person"], "person_id": result.get("person_id")}, "name"
        )
    movie = result.get("movie") if isinstance(result.get("movie"), dict) else None
    if movie:
        for person in (movie.get("directors") or [])[:1]:
            name = person.get("name") if isinstance(person, dict) else person
            if name:
                state["people"] = _remember(state["people"], {"name": name, "person_id": None}, "name")
        for member in (movie.get("cast") or [])[:3]:
            if isinstance(member, dict) and member.get("character"):
                state["characters"] = _remember(state["characters"], str(member["character"])[:80])
    for key in ("story_locations", "filming_locations"):
        for place in (result.get(key) or [])[:4]:
            state["places"] = _remember(state["places"], str(place)[:80])


def record_comparison(state: dict[str, Any], films: list[dict[str, Any]], aspects: list[str]) -> None:
    """The films being compared, with year and runtime, for "which one is longer?"."""
    kept = []
    for film in films:
        if not isinstance(film, dict) or not film.get("title"):
            continue
        year = str(film.get("year") or film.get("release_date") or "")[:4]
        kept.append(
            {
                "title": str(film["title"]),
                "year": int(year) if year.isdigit() else None,
                "tmdb_id": film.get("tmdb_id"),
                "runtime": film.get("runtime") if isinstance(film.get("runtime"), int) else None,
            }
        )
    if not kept:
        return
    state["comparison"] = {
        # With the year, so "Dune (2021)" is not re-asked as "which Dune?" on a follow-up.
        "titles": [f"{f['title']} ({f['year']})" if f["year"] else f["title"] for f in kept],
        "dimensions": aspects,
        "films": kept,
    }


def set_pending(state: dict[str, Any], kind: str, question: str, **data: Any) -> None:
    state["pending"] = {"kind": kind, "question": question, "data": data}


def ambiguous_candidates(result: Any) -> list[dict[str, Any]]:
    """Normalise 'which one did you mean?' candidates from a lookup result."""
    if not isinstance(result, dict) or result.get("status") != "ambiguous":
        return []
    out = []
    for item in result.get("candidates") or []:
        if isinstance(item, dict) and item.get("title"):
            out.append({"title": item["title"], "year": str(item.get("release_date") or "")[:4] or None})
        elif isinstance(item, str):
            match = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", item)
            out.append(
                {"title": match.group(1), "year": match.group(2)} if match else {"title": item, "year": None}
            )
    return out[:6]
