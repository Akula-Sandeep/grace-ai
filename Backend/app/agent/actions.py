"""Movie-list actions from conversation (Jarvis spec §20-§21, §34).

The model (or the deterministic parser) identifies intent; this module decides
*which* film is meant, performs the change through the application's own
services, reads the result back, and only then confirms it:

    resolve target -> mutate -> verify -> confirm -> tell the UI what changed

A confirmation is never written unless the database (or, for guests, the
session store) shows the change. "Add it" after a list of five films asks which
one rather than guessing, and "the second one" / "both" / "Argo" all resolve
against what Grace just showed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.movie_state import MovieStateService
from app.observability import logger
from app.services.upstream_errors import UpstreamError
from app.shared_state import shared_state

LIST_INTENTS = frozenset(
    {
        "add_to_watchlist",
        "remove_from_watchlist",
        "mark_as_watched",
        "view_watchlist",
        "set_reminder",
        "rate_movie",
        "unmark_watched",
    }
)
_VERBS = {
    "add_to_watchlist": "add",
    "remove_from_watchlist": "remove",
    "mark_as_watched": "mark as watched",
    "unmark_watched": "unmark",
    "set_reminder": "set a reminder for",
    "rate_movie": "rate",
}
_GUEST_TTL = 24 * 60 * 60
# Failures of one lookup or write: reported to the user, never confirmed.
_FAILURES = (
    httpx.HTTPError,
    SQLAlchemyError,
    UpstreamError,
    ValueError,
    KeyError,
    TypeError,
    RuntimeError,
    OSError,
    TimeoutError,
)


def state_event(changed: list[str], **data: Any) -> str:
    """Tell the client that the user's lists changed, so open pages refresh."""
    payload = {"changed": changed, **data}
    return "event: state\n" f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _join(names: list[str], word: str = "and") -> str:
    """Names are already formatted (bold title, optional year)."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {word} " + names[-1]


def _label(details: dict[str, Any], fallback: str) -> str:
    title = details.get("title") or fallback
    year = str(details.get("release_date") or "")[:4]
    return f"**{title}** ({year})" if year.isdigit() else f"**{title}**"


async def _stored_film(tmdb_id: int, db_factory) -> dict[str, Any] | None:
    """The film as this app already stored it (title, year, runtime), or None."""
    if db_factory is None:
        return None
    from sqlalchemy import select

    from app.database import Movie, run_db

    def load(db):
        row = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
        if row is None or not (row.title or "").strip():
            return None
        details = dict(row.metadata_json) if isinstance(row.metadata_json, dict) else {}
        details.setdefault("id", tmdb_id)
        details.setdefault("title", row.title)
        if row.release_year and not details.get("release_date"):
            details["release_date"] = f"{row.release_year}-01-01"
        if row.runtime and not details.get("runtime"):
            details["runtime"] = row.runtime
        return details

    try:
        return await run_db(load, session_factory=db_factory)
    except SQLAlchemyError as exc:
        logger.warning("action_stored_film_failed tmdb_id=%s error=%r", tmdb_id, exc)
        return None


async def _resolve(engine, target: dict[str, Any], db_factory=None) -> tuple[dict[str, Any] | None, Any]:
    """(details, lookup) for a target; a known TMDB id skips the title search."""
    tmdb_id = target.get("tmdb_id")
    if tmdb_id:
        try:
            details = await engine.tmdb_client.get_movie_details(int(tmdb_id))
            if isinstance(details, dict) and details.get("id"):
                return details, {"status": "found", "details": details}
        except _FAILURES as exc:  # fall back to the stored film, then a title search
            logger.warning("action_details_failed tmdb_id=%s error=%r", tmdb_id, exc)
            stored = await _stored_film(int(tmdb_id), db_factory)
            if stored is not None:
                # A film already known by its id needs no movie database to act on
                # (a confirmed action must not fail because TMDB is unreachable).
                return stored, {"status": "found", "details": stored}
    lookup = await engine.resolve_movie(target["title"], year=target.get("year"))
    details = lookup.get("details") if isinstance(lookup, dict) else None
    if (
        not isinstance(lookup, dict)
        or lookup.get("status") != "found"
        or not isinstance(details, dict)
        or not details.get("id")
    ):
        return None, lookup
    return details, lookup


async def _guest_list(guest_session_id: str) -> list[dict[str, Any]]:
    saved = await shared_state.cache_get(f"guest-watchlist:{guest_session_id}", _GUEST_TTL)
    return saved if isinstance(saved, list) else []


async def run_action(
    classification,
    user_message: str,
    *,
    state: dict[str, Any],
    db=None,
    db_factory=None,
    user_id: int | None = None,
    guest_session_id: str | None = None,
    movie_engine=None,
    movie_reference_callback=None,
    user_timezone: str | None = None,
    targets: list[dict[str, Any]] | None = None,
    note: str | None = None,
    report: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Perform one list action and confirm it only after reading it back.

    ``targets`` (title / year / tmdb_id dicts) are the films the model named in its
    tool call (or the film of a confirmed proposal); this pipeline never guesses them
    from the user's words. ``report`` receives what happened, so a caller can tell
    "no such film" apart from a real outcome.
    """
    from app.agent import working_state as working
    from app.agent.tool_support import _schedule_reminder, _unresolved_title_message

    report = report if report is not None else {}
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    intent = classification.intent
    signed_in = user_id is not None and (db is not None or db_factory is not None)
    if not signed_in and (
        not guest_session_id or intent in {"set_reminder", "rate_movie", "mark_as_watched", "unmark_watched"}
    ):
        yield {
            "set_reminder": "Sign in so I can keep reminders for you.",
            "rate_movie": "Sign in so I can save your ratings.",
        }.get(intent, "Sign in to manage your watchlist and watched movies.")
        return

    # ---- view -------------------------------------------------------------
    if intent == "view_watchlist":
        if signed_in:
            items = await MovieStateService(db, None, db_factory=db_factory)._list_watchlist_async(user_id)
            names = [
                f"**{item['title']}**" + (f" ({item['release_year']})" if item.get("release_year") else "")
                for item in items
            ]
        else:
            names = [
                f"**{item['movie_title']}**"
                for item in await _guest_list(str(guest_session_id))
                if item.get("movie_title")
            ]
        if not names:
            yield "Your watchlist is empty. Say “add Dune to my watchlist”, or ask me for something to watch."
            return
        if movie_reference_callback and signed_in:
            movie_reference_callback(
                [{"tmdb_id": item["tmdb_id"], "title": item["title"]} for item in items[:12]]
            )
        listed = "\n".join(f"- {name}" for name in names[:30])
        more = f"\n\n…and {len(names) - 30} more." if len(names) > 30 else ""
        yield f"Your watchlist ({len(names)}):\n\n{listed}{more}"
        return

    # ---- reminder without a film ---------------------------------------------
    engine = movie_engine or MovieDataFallbackEngine()
    owns_engine = movie_engine is None
    targets = list(targets or [])
    if intent == "set_reminder" and not targets:
        from app.agent import ledger

        if owns_engine:
            await engine.close()

        # A reminder with no film ("remind me to pick something on Friday"): once per request too.
        key = ledger.action_key("set_reminder", None, classification.reminder_when or user_message)
        previous = await ledger.recall(key, db_factory=db_factory)
        if previous:
            yield str((previous.get("extra") or [""])[0])
            return
        message = await _schedule_reminder(
            db, db_factory, user_id, None, None, classification, user_message, user_timezone, note=note
        )
        if message.startswith("I'll remind you"):
            await ledger.remember(key, {"outcome": "changed", "extra": [message]}, db_factory=db_factory)
            yield state_event(["reminders"])
        yield message
        return
    if not targets:
        if owns_engine:
            await engine.close()
        yield "Which film do you mean?"
        return
    from app.agent import ledger
    from app.agent.turn import current_turn

    turn = current_turn()
    done: list[str] = []
    moved: list[str] = []
    already: list[str] = []
    problems: list[str] = []
    resolved_refs: list[dict[str, Any]] = []
    extra: list[str] = []
    undo_links: list[str] = []
    upstream_failures: list[BaseException] = []
    changed: set[str] = set()
    try:
        movie_state = MovieStateService(db, engine.tmdb_client, db_factory=db_factory) if signed_in else None
        for target in targets:
            try:
                details, lookup = await _resolve(engine, target, db_factory)
            except _FAILURES as exc:  # one bad title must not hide the others
                logger.warning("action_lookup_failed title=%r error=%r", target.get("title"), exc)
                if isinstance(exc, UpstreamError):
                    upstream_failures.append(exc)
                    problems.append(
                        f"I can't reach the movie database right now, so **{target.get('title')}** wasn't changed."
                    )
                else:
                    problems.append(
                        f"I couldn't look up **{target.get('title')}** just now, so it wasn't changed."
                    )
                continue
            if details is None:
                candidates = working.ambiguous_candidates(lookup) if isinstance(lookup, dict) else []
                report["unresolved"] = report.get("unresolved", 0) + (0 if candidates else 1)
                problems.append(_unresolved_title_message(target.get("title") or "that film", lookup, intent))
                if candidates:
                    working.set_pending(
                        state,
                        "choose_movie",
                        "Which film the user means",
                        candidates=candidates,
                        intent=intent,
                    )
                continue
            tmdb_id = int(details["id"])
            name = _label(details, target.get("title") or "")
            resolved_refs.append({"tmdb_id": tmdb_id, "title": details.get("title") or target.get("title")})
            reminding = intent == "set_reminder" or (
                intent == "add_to_watchlist" and bool(classification.reminder_when)
            )
            key = ledger.action_key(
                intent, tmdb_id, (classification.reminder_when or "remind") if reminding else None
            )
            previous = await ledger.recall(key, db_factory=db_factory)
            if previous:
                # This request already did it (a retry, regenerate or double send):
                # report the original outcome; never act twice.
                (done if previous.get("outcome") == "changed" else already).append(
                    previous.get("name") or name
                )
                if previous.get("moved"):
                    moved.append(previous.get("name") or name)
                extra.extend(str(item) for item in previous.get("extra") or [])
                if previous.get("undo"):
                    undo_links.append(str(previous["undo"]))
                if turn is not None:
                    turn.fallback("action_replayed")
                continue
            target_extra: list[str] = []
            undo_link: str | None = None
            try:
                if not signed_in:
                    outcome = await _guest_mutation(str(guest_session_id), intent, details, tmdb_id)
                else:
                    outcome = await _user_mutation(
                        movie_state, intent, int(user_id or 0), tmdb_id, classification, user_message
                    )
            except _FAILURES as exc:  # reported to the user, never confirmed
                logger.warning(
                    "native_movie_state_update_failed intent=%s tmdb_id=%s error=%r", intent, tmdb_id, exc
                )
                problems.append(
                    f"I couldn't update {name} just now, so nothing was changed. Please try again."
                )
                continue
            if outcome == "ask_rating":
                working.set_pending(state, "rating", "What rating to give", title=details.get("title"))
                problems.append(f"What would you rate {name} out of 10?")
                continue
            if outcome == "unverified":
                problems.append(f"I tried to update {name}, but couldn't confirm it saved. Please try again.")
                continue
            if (
                intent == "add_to_watchlist"
                and outcome == "changed"
                and len(targets) == 1
                and details.get("runtime")
            ):
                # The confirmation reports the action, with the film's verified year and runtime.
                # It no longer adds where the film streams: that lookup used the server's default
                # region, not the user's (owner, 2026-10-15: "It's streaming on Netflix" for a
                # user in India). Where to watch is a follow-up, answered for the user's region.
                name = (
                    name[:-1] + f", {int(details['runtime'])} min)"
                    if name.endswith(")")
                    else f"{name} ({int(details['runtime'])} min)"
                )
            if outcome == "moved":
                moved.append(name)
                changed.add("watchlist")
                outcome = "changed"
            (done if outcome == "changed" else already).append(name)
            if outcome == "changed" and intent == "remove_from_watchlist":
                undo = "guest-watchlist" if not signed_in else "watchlist"
                undo_link = f"[Undo](Grace://{undo}/undo/{tmdb_id})"
                undo_links.append(undo_link)
            changed.add(
                "watched"
                if intent in {"mark_as_watched", "unmark_watched"}
                else "ratings" if intent == "rate_movie" else "watchlist"
            )
            if signed_in and reminding:
                target_extra.append(
                    await _schedule_reminder(
                        db,
                        db_factory,
                        user_id,
                        tmdb_id,
                        details.get("title"),
                        classification,
                        user_message,
                        user_timezone,
                    )
                )
                changed.add("reminders")
            extra.extend(target_extra)
            if outcome in {"changed", "already"}:
                await ledger.remember(
                    key,
                    {
                        "outcome": outcome,
                        "name": name,
                        "extra": target_extra,
                        "undo": undo_link,
                        "tmdb_id": tmdb_id,
                        "moved": name in moved,
                    },
                    db_factory=db_factory,
                )
    finally:
        if owns_engine:
            await engine.close()

    if upstream_failures and len(upstream_failures) == len(targets):
        # Nothing could even be looked up: a retryable error (with "Try again")
        # says this better than a sentence, and nothing was changed.
        raise upstream_failures[-1]
    report["targets"] = len(targets)
    report["changed"] = len(done)
    report["already"] = len(already)
    report["failed"] = bool(targets) and not done and not already
    if resolved_refs and movie_reference_callback:
        movie_reference_callback(resolved_refs)
    message = _confirmation(
        intent, [d for d in done if d not in moved], already, classification, signed_in=signed_in, moved=moved
    )
    if message and (done or already) and intent != "view_watchlist":
        # The completed action, for this conversation's next turn: a "yes" after it means
        # "it's done", never a second change (confirmations.py).
        state["last_action"] = {
            "intent": intent,
            "summary": message,
            "tmdb_ids": [ref["tmdb_id"] for ref in resolved_refs],
        }
        # What the model is told about it on later turns, until the next change.
        state["recent_action"] = dict(state["last_action"])
    parts = [part for part in (message, *undo_links[:1], *extra, *problems) if part]
    if any("When should I remind you?" in part for part in extra) and resolved_refs:
        working.set_pending(
            state, "reminder_time", "When to send the reminder", title=resolved_refs[0]["title"]
        )
    if changed and (done or intent == "set_reminder"):
        yield state_event(sorted(changed), tmdb_ids=[ref["tmdb_id"] for ref in resolved_refs])
    yield " ".join(parts) if parts else "I couldn't make that change. Please try again."


async def _guest_mutation(guest_session_id: str, intent: str, details: dict[str, Any], tmdb_id: int) -> str:
    key = f"guest-watchlist:{guest_session_id}"
    saved = await _guest_list(guest_session_id)
    present = any(int(item.get("tmdb_id", -1)) == tmdb_id for item in saved)
    if intent == "remove_from_watchlist":
        if not present:
            return "absent"
        await shared_state.cache_set(
            f"guest-watchlist-undo:{guest_session_id}:{tmdb_id}",
            next(item for item in saved if int(item.get("tmdb_id", -1)) == tmdb_id),
            settings.cache_max_entries,
            ttl=30,
        )
        saved = [item for item in saved if int(item.get("tmdb_id", -1)) != tmdb_id]
        await shared_state.cache_set(key, saved, settings.cache_max_entries, ttl=_GUEST_TTL)
        after = await _guest_list(guest_session_id)
        return "unverified" if any(int(i.get("tmdb_id", -1)) == tmdb_id for i in after) else "changed"
    if present:
        return "already"
    saved.append(
        {
            "status": "added",
            "movie_title": details.get("title"),
            "tmdb_id": tmdb_id,
            "runtime_minutes": details.get("runtime"),
        }
    )
    await shared_state.cache_set(key, saved, settings.cache_max_entries, ttl=_GUEST_TTL)
    after = await _guest_list(guest_session_id)
    return "changed" if any(int(i.get("tmdb_id", -1)) == tmdb_id for i in after) else "unverified"


async def _user_mutation(
    movie_state, intent: str, user_id: int, tmdb_id: int, classification, user_message: str
) -> str:
    """Perform one change and read it back. Returns changed|already|absent|unverified|ask_rating."""
    if intent in {"add_to_watchlist", "set_reminder"}:
        if intent == "set_reminder":
            # A reminder about a film also keeps it on the watchlist.
            await movie_state.add_watchlist(user_id, tmdb_id)
            return "changed"
        result = await movie_state.add_watchlist(user_id, tmdb_id)
        on_list = await _on_watchlist(movie_state, user_id, tmdb_id)
        if not on_list:
            return "unverified"
        return (
            "already"
            if result.get("status") == "already_present" or result.get("already_present")
            else "changed"
        )
    if intent == "remove_from_watchlist":
        result = await movie_state.remove_watchlist(user_id, tmdb_id)
        if result.get("status") != "removed":
            return "absent"
        return "unverified" if await _on_watchlist(movie_state, user_id, tmdb_id) else "changed"
    if intent == "mark_as_watched":
        # Watched films leave the watchlist: one atomic move (owner, 2026-10-15: "no, to
        # watchedlist" after an add must not leave the film on both lists).
        was_listed = await _on_watchlist(movie_state, user_id, tmdb_id)
        result = await movie_state.move_to_watched(user_id, tmdb_id)
        if not await _on_watched(movie_state, user_id, tmdb_id) or await _on_watchlist(
            movie_state, user_id, tmdb_id
        ):
            return "unverified"
        if result.get("status") == "already_present":
            return "already"
        return "moved" if was_listed else "changed"
    if intent == "unmark_watched":
        result = await movie_state.unmark_watched(user_id, tmdb_id)
        return "changed" if result.get("status") == "removed" else "absent"
    if intent == "rate_movie":
        rating = classification.rating
        if rating is None:
            return "ask_rating"
        await movie_state.rate(user_id, tmdb_id, rating)
        return "changed"
    return "unverified"


async def _on_watchlist(movie_state, user_id: int, tmdb_id: int) -> bool:
    items = await movie_state._list_watchlist_async(user_id)
    return any(int(item.get("tmdb_id") or -1) == tmdb_id for item in items)


async def _on_watched(movie_state, user_id: int, tmdb_id: int) -> bool:
    items = await movie_state._db_call(lambda db: movie_state.__class__(db, None).list_watched(user_id))
    return any(int(item.get("tmdb_id") or -1) == tmdb_id for item in items)


def _confirmation(
    intent: str,
    done: list[str],
    already: list[str],
    classification,
    *,
    signed_in: bool,
    moved: list[str] | None = None,
) -> str:
    moved = moved or []
    parts = []
    if intent == "add_to_watchlist":
        if done:
            parts.append(f"Added {_join(done)} to your watchlist.")
        if already:
            parts.append(
                f"{_join(already)} {'is' if len(already) == 1 else 'are'} already on your watchlist."
            )
        if done and not signed_in:
            parts.append(
                "It's saved for this guest session. Create a free account to keep it; it carries over."
            )
    elif intent == "remove_from_watchlist":
        if done:
            parts.append(f"Removed {_join(done)} from your watchlist.")
        if already:
            parts.append(f"{_join(already)} wasn’t on your watchlist.")
    elif intent == "mark_as_watched":
        if moved:
            parts.append(f"Moved {_join(moved)} from your watchlist to your watched list.")
        if done:
            parts.append(f"Marked {_join(done)} as watched. I'll keep it out of your recommendations.")
        if already:
            parts.append(f"{_join(already)} is already marked as watched.")
    elif intent == "unmark_watched":
        if done:
            parts.append(f"Removed {_join(done)} from your watched list.")
        if already:
            parts.append(f"{_join(already)} wasn’t marked as watched.")
    elif intent == "rate_movie" and done:
        rating = classification.rating
        score = f" {rating:g}/10" if rating is not None else ""
        parts.append(f"Rated {_join(done)}{score}. I'll use that for future recommendations.")
    elif intent == "set_reminder" and done:
        parts.append(f"{_join(done)} is on your watchlist.")
    return " ".join(parts)
