"""What to offer under an answer: follow-up suggestions and one-tap answers to a question.

Both come from the working state saved with the reply, never from the model, so
every chip is something the agent understands and can act on:

* **Options** answer the question the reply asked ("Which Dune did you mean?",
  "When should I remind you?"). Tapping one sends it as the next message, and the
  pending-question resolver in ``working_state.apply`` reads it.
* **Suggestions** are the natural next steps for what the reply was about: the film
  in focus, the list just shown, or the films just compared.
"""

from __future__ import annotations

from typing import Any

MAX_SUGGESTIONS = 3
MAX_OPTIONS = 6

REMINDER_TIMES = ("Tonight at 8 pm", "Tomorrow evening", "This weekend")
RATING_CHOICES = ("10", "8", "6", "4")

# Intents whose reply is a confirmation or a personal answer: suggesting more there is noise.
_QUIET_INTENTS = frozenset(
    {
        "action",
        "add_to_watchlist",
        "remove_from_watchlist",
        "mark_as_watched",
        "unmark_watched",
        "rate_movie",
        "set_reminder",
        "preference_update",
        "movie_feedback",
        "coding_math",
        "casual_chat",
    }
)


def _label(film: dict[str, Any]) -> str:
    title = str(film.get("title") or "").strip()
    year = film.get("year")
    return f"{title} ({year})" if title and str(year or "").isdigit() else title


def options_for(state: dict[str, Any] | None) -> list[dict[str, str]]:
    """One-tap answers to the question the reply ended on, or none."""
    pending = (state or {}).get("pending") or {}
    if not isinstance(pending, dict):
        return []
    data = pending.get("data") or {}
    kind = pending.get("kind")
    out: list[dict[str, str]] = []
    if kind == "choose_movie":
        seen = set()
        for film in data.get("candidates") or []:
            if not isinstance(film, dict):
                continue
            label = _label(film)
            if label and label.casefold() not in seen:
                seen.add(label.casefold())
                out.append({"label": label, "message": label})
    elif kind == "reminder_time" and data.get("title"):
        out = [{"label": when, "message": when} for when in REMINDER_TIMES]
    elif kind == "rating" and data.get("title"):
        out = [{"label": f"{value}/10", "message": f"{value}/10"} for value in RATING_CHOICES]
    return out[:MAX_OPTIONS]


def suggestions_for(
    state: dict[str, Any] | None,
    *,
    intent: str | None = None,
    user_message: str = "",
    signed_in: bool = False,
) -> list[dict[str, str]]:
    """Up to three next steps for what this reply was about."""
    state = state or {}
    if options_for(state) or intent in _QUIET_INTENTS:
        return []
    films = [f for f in state.get("turn_movies") or [] if isinstance(f, dict) and f.get("title")]
    comparison = state.get("comparison") or {}
    out: list[tuple[str, str]] = []
    if intent == "compare" and len(comparison.get("films") or []) >= 2:
        out = [
            ("Which is longer?", "Which one is longer?"),
            ("Which is rated higher?", "Which one is rated higher?"),
            ("Where can I watch them?", "Where can I watch both of them?"),
        ]
    elif len(films) >= 2 and state.get("turn_listed"):
        first, second = films[0]["title"], films[1]["title"]
        out = [
            (f"More about {first}", f"Tell me more about {first}"),
            (f"Compare {first} and {second}", f"Compare {first} and {second}"),
            ("Something different", "Show me something different from these"),
        ]
    elif len(films) == 1 or (state.get("focus") and not films and intent in {"metadata", "general_fact"}):
        film = films[0] if films else state["focus"]
        title = str(film.get("title") or "")
        if title:
            out = [
                ("Where to watch", f"Where can I watch {title}?"),
                ("Films like this", f"Recommend films like {title}"),
                (
                    ("Add to watchlist", f"Add {_label(film)} to my watchlist")
                    if signed_in
                    else ("Who's in it", f"Who's in {title}?")
                ),
            ]
    asked = " ".join((user_message or "").casefold().split())
    picked = []
    for label, message in out:
        if " ".join(message.casefold().split()) == asked:
            continue
        picked.append({"label": label, "message": message})
    return picked[:MAX_SUGGESTIONS]


def followups(
    state: dict[str, Any] | None,
    *,
    intent: str | None = None,
    user_message: str = "",
    signed_in: bool = False,
) -> dict[str, list[dict[str, str]]]:
    return {
        "options": options_for(state),
        "suggestions": suggestions_for(state, intent=intent, user_message=user_message, signed_in=signed_in),
    }
