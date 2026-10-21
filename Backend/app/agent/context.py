"""What the answering model is told about the conversation, within a token budget.

Three parts, each bounded:

* **History.** As many recent turns as fit the budget, newest first, so a fact
  stated twenty messages ago is still in view when the budget allows. Only
  older assistant replies are shortened; the user's own words are kept.
* **Conversation state.** The working state as a short, typed block: the film
  in focus, the last list shown (numbered, with TMDB ids), the comparison, the
  active recommendation request and any question Grace is waiting on. This is
  what "it", "the second one" and "make it shorter" resolve against.
* **About the user.** Name, saved taste, relevant memories and the rolling
  summary of older turns, for signed-in users who allow memory.

Token counts are estimated (about 3.5 characters per token, conservative for
non-English text); the provider's own limit errors are still handled by the
model chain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 4


def _shorten(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def budget_history(
    history: list | None,
    token_budget: int,
    *,
    keep_last: int = 2,
    old_reply_chars: int = 1800,
    max_messages: int = 40,
) -> list[dict[str, str]]:
    """The most recent user/assistant messages that fit ``token_budget``, oldest first."""
    entries = [
        {"role": e["role"], "content": e["content"]}
        for e in (history or [])
        if isinstance(e, dict)
        and e.get("role") in {"user", "assistant"}
        and isinstance(e.get("content"), str)
        and e["content"].strip()
    ][-max_messages:]
    chosen: list[dict[str, str]] = []
    used = 0
    for position, entry in enumerate(reversed(entries)):
        content = entry["content"]
        if position >= keep_last and entry["role"] == "assistant":
            content = _shorten(content, old_reply_chars)
        elif len(content) > 6000:
            content = _shorten(content, 6000)
        cost = estimate_tokens(content)
        if used + cost > token_budget and position >= keep_last:
            break
        chosen.append({"role": entry["role"], "content": content})
        used += cost
    chosen.reverse()
    return chosen


def _film(movie: dict[str, Any]) -> str:
    title = str(movie.get("title") or "?")
    year = movie.get("year")
    label = f"**{title}**" + (f" ({year})" if year else "")
    if movie.get("tmdb_id"):
        label += f" [tmdb {movie['tmdb_id']}]"
    return label


def state_block(state: dict[str, Any] | None) -> str:
    """The working state as lines the model can resolve references against."""
    if not state:
        return ""
    lines: list[str] = []
    focus = state.get("focus")
    if isinstance(focus, dict) and focus.get("title"):
        lines.append(f"Film in focus (“it”, “that film”): {_film(focus)}")
    shown = [m for m in state.get("last_answer_movies") or [] if isinstance(m, dict) and m.get("title")]
    if shown:
        numbered = "; ".join(f"{i}. {_film(m)}" for i, m in enumerate(shown[:8], 1))
        lines.append(f"Films in Grace's last list, in the order shown (“the second one”): {numbered}")
    handled = [h for h in state.get("handled") or [] if h]
    if handled:
        lines.append("Already acted on from that list: " + ", ".join(f"**{h}**" for h in handled[:6]))
    movies = [m for m in state.get("movies") or [] if isinstance(m, dict) and m.get("title")]
    if movies:
        lines.append("Films discussed recently: " + ", ".join(_film(m) for m in movies[-6:]))
    people = [p for p in state.get("people") or [] if isinstance(p, dict) and p.get("name")]
    if people:
        lines.append("People discussed (“he”, “she”): " + ", ".join(p["name"] for p in people[-4:]))
    comparison = state.get("comparison")
    if isinstance(comparison, dict) and comparison.get("titles"):
        aspects = ", ".join(comparison.get("dimensions") or [])
        films = [f for f in comparison.get("films") or [] if isinstance(f, dict) and f.get("title")]
        named = (
            [_film(f) + (f", {f['runtime']} min" if isinstance(f.get("runtime"), int) else "") for f in films]
            if films
            else [f"**{t}**" for t in comparison["titles"]]
        )
        lines.append(
            "Active comparison (“which is longer?”): "
            + " vs ".join(named)
            + (f" on {aspects}" if aspects else "")
        )
    request = state.get("active_request")
    if isinstance(request, dict):
        constraints = {k: v for k, v in request.items() if v not in (None, [], False, "")}
        if constraints:
            lines.append(
                "Recommendation request still in force (keep these unless the user changes them): "
                + "; ".join(f"{k}={v}" for k, v in constraints.items())
            )
    pending = state.get("pending")
    proposal = (pending.get("data") or {}) if isinstance(pending, dict) else {}
    if isinstance(pending, dict) and pending.get("kind") == "confirm_action" and proposal.get("target"):
        # A list change Grace proposed: the user's next message answers it (yes, no,
        # a correction, or something else entirely). Only a yes should perform it.
        lines.append(
            "Proposed list change waiting for the user's answer: "
            f"{proposal.get('intent')} {_film(proposal['target'])}"
            ". If the user agrees, make it with update_my_list; if they decline or change it, follow them."
        )
    elif isinstance(pending, dict) and pending.get("question"):
        line = f"Grace asked and is waiting for an answer: “{pending['question']}”"
        candidates = (pending.get("data") or {}).get("candidates") or []
        if candidates:
            line += " Options: " + "; ".join(
                f"{i}. {_film(c)}" for i, c in enumerate(candidates[:6], 1) if isinstance(c, dict)
            )
        lines.append(line)
    done = state.get("recent_action") or state.get("last_action") or state.get("previous_action")
    if isinstance(done, dict) and done.get("summary"):
        lines.append(
            "Most recent list change in this conversation, already done (don't redo it; a yes "
            f"to it means it's done): {done['summary']}"
        )
    else:
        # The ground truth the model needs to never claim a change it did not make.
        lines.append("List changes made in this conversation so far: none.")
    if not lines:
        return ""
    return "Conversation state (use it to resolve references; it may be incomplete):\n- " + "\n- ".join(lines)


# The user's answer-length setting (Settings > Personalisation); "balanced" adds nothing.
_ANSWER_STYLES = {
    "brief": "They chose brief answers in their settings: keep replies short and to the point "
    "unless this message asks for depth.",
    "detailed": "They chose detailed answers in their settings: give fuller explanations and "
    "context unless this message asks for something short.",
}


def user_block(
    *,
    name: str | None = None,
    signed_in: bool,
    memory_enabled: bool = True,
    preferences: list[dict[str, Any]] | None = None,
    memories: list[str] | None = None,
    memories_omitted: int = 0,
    summary: str | None = None,
    region: str | None = None,
    answer_style: str | None = None,
    char_limit: int = 3600,
) -> str:
    lines: list[str] = []
    if not signed_in:
        lines.append(
            "The user is a guest (not signed in). Their watchlist lasts for this session; "
            "ratings, watched films and reminders need a free account."
        )
    else:
        if name:
            lines.append(f"The user's name: {name}.")
        if region:
            lines.append(f"Their region for availability: {region}.")
        if not memory_enabled:
            lines.append("The user turned memory off: don't claim to remember them between conversations.")
        style = _ANSWER_STYLES.get(answer_style or "")
        if style:
            lines.append(style)
    likes = [p["value"] for p in preferences or [] if p.get("polarity") == "prefer" and p.get("value")]
    dislikes = [p["value"] for p in preferences or [] if p.get("polarity") == "avoid" and p.get("value")]
    if likes:
        lines.append("Saved taste — likes: " + ", ".join(dict.fromkeys(likes[:12])) + ".")
    if dislikes:
        lines.append("Saved taste — avoids: " + ", ".join(dict.fromkeys(dislikes[:12])) + ".")
    if memories:
        lines.append(
            "Things the user told Grace before (use only those that apply to this message, "
            "even when worded differently or in another language):\n"
            + "\n".join(f"  - {_shorten(m, 200)}" for m in memories[:16])
        )
        if memories_omitted:
            lines.append(
                f"{memories_omitted} older saved memories are not listed. If one might matter here, call "
                "recall_conversation with a few English keywords the user would have used."
            )
    if summary:
        lines.append("Summary of earlier parts of this conversation:\n" + _shorten(summary, 1400))
    if not lines:
        return ""
    return _shorten("About the user and this conversation:\n- " + "\n- ".join(lines), char_limit)


def today_line(user_timezone: str | None, now: datetime | None = None) -> str:
    """The current date and time, so the model doesn't treat its training cutoff as today."""
    from app.services.reminders import user_zone

    zone = user_zone(user_timezone)
    local = (now or datetime.now(UTC)).astimezone(zone)
    return f"Today is {local.strftime('%A %d %B %Y')}, {local.strftime('%H:%M')} in the user's time zone ({zone.key})."
