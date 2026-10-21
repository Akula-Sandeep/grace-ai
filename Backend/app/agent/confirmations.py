"""Yes / no to a proposed list change when no model can read the reply.

A proposal is stored as structured data when the model calls ``update_my_list``
with ``ask_first`` (or when a change was held because the model had read tool
results): the operation, the film with its TMDB id, and any rating or time. When
a model is available it reads the user's answer itself. Only when none can be
reached does ``reply_kind`` decide whether the whole message is a plain yes or no,
so a change the user confirmed is still made (or cancelled) with every model down:

* a plain yes ("yes", "yesssss", "just do it") performs exactly that action;
* a plain no cancels it;
* anything else drops it, so a later "yes" can never act on a stale question.
"""

from __future__ import annotations

import re
from typing import Any

# A whole message that only says yes / no (with harmless extras: "yes please", "ok do it").
_YES_WORD = (
    r"y|y+e+[sa]+h*|y+e+p+|y+u+p+|y+a+h*|yea+h*|ye+|sure|ok(?:ay)?|k+|alright|all right|"
    r"go ahead|go for it|(?:just\s+)?do it|please do|please|confirm(?:ed)?|correct|right|"
    r"that'?s right|absolutely|of course|definitely|sounds good|perfect|do that|yes do it|"
    r"try again|retry|again"
)
_YES = re.compile(
    rf"^\s*(?:{_YES_WORD})(?:[\s,.!]+(?:{_YES_WORD}|now|thanks|thank you|mate|man|bro))*[\s.!]*$",
    re.IGNORECASE,
)
_NO_WORD = r"no+|nope|nah|don'?t|do not|cancel|never\s*mind|stop|leave it|forget it|no thanks|not now|skip it"
_NO = re.compile(
    rf"^\s*(?:{_NO_WORD})(?:[\s,.!]+(?:{_NO_WORD}|thanks|thank you))*[\s.!]*$",
    re.IGNORECASE,
)


def reply_kind(message: str) -> str | None:
    """ "yes" / "no" when the whole message only answers a yes-or-no question, else None."""
    text = (message or "").strip()
    if not text or len(text) > 60:
        return None
    if _NO.match(text):
        return "no"
    if _YES.match(text):
        return "yes"
    return None


def pending_confirmation(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = state.get("pending") or {}
    if not isinstance(pending, dict) or pending.get("kind") != "confirm_action":
        return None
    data = pending.get("data") or {}
    target = data.get("target") or {}
    if (
        not data.get("intent")
        or not isinstance(target, dict)
        or not (target.get("title") or target.get("tmdb_id"))
    ):
        return None
    return {
        "intent": data["intent"],
        "target": target,
        "question": pending.get("question"),
        "rating": data.get("rating"),
        "reminder_when": data.get("reminder_when"),
    }
