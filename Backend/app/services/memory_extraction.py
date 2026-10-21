"""Durable facts the user stated, extracted by a model (audit MEM-002).

A small model reads one user message (after a chat turn, or written feedback) and
returns short, third-person facts the user *stated* about themselves ("I watch with
my grandmother", "I only follow dubbed Hindi versions", "call me Priya"). Nothing is
inferred, questions and requests are ignored, and sensitive categories are never
stored (the model is told so, and a privacy filter drops any that slip through). The
result is shown and editable under My taste like every other memory. Nothing here is
required for a turn to work.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.inference.errors import MODEL_CALL_FAILURES
from app.inference.models import InferenceRequest
from app.observability import log_event

_MAX_FACTS = 4

_PROMPT = (
    "You extract durable facts a user stated about themselves, for an assistant's long-term memory.\n"
    'Return JSON: {"facts": [{"text": "...", "kind": "preference|constraint|profile"}]}.\n'
    "Rules:\n"
    "- Only facts the user explicitly stated about themselves that stay true for weeks: their name or what to "
    "call them, who they usually watch with, languages they understand or prefer (dubbed vs subtitles), "
    "accessibility needs, stable likes and dislikes (films or anything else), recurring routines.\n"
    '- Write each fact in third person, short and specific, in English ("Prefers dubbed Hindi versions over '
    'subtitles").\n'
    "- Nothing temporary (tonight, this week, right now), no questions, no requests, no guesses.\n"
    "- Never include health, religion, politics, sexuality, finances, contact details, ID numbers, or anything "
    "about children.\n"
    '- If there is nothing durable, return {"facts": []}.'
)
_SENSITIVE = re.compile(
    r"\b(?:diagnos|disease|illness|depress|anxiety|medication|pregnan|religio|muslim|christian|hindu|jewish|"
    r"politic|vote[sd]?|democrat|republican|gay|lesbian|bisexual|sexual|salary|income|debt|bank|password|"
    r"phone number|address|passport|social security|my (?:son|daughter|kid|child)\b)",
    re.IGNORECASE,
)


def worth_extracting(message: str) -> bool:
    """Whether a message is long enough to hold a fact; what it says is the model's call."""
    text = (message or "").strip()
    return 12 <= len(text) <= 2000


def parse_facts(text: str | None) -> list[dict[str, str]]:
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    facts: list[dict[str, str]] = []
    for item in payload.get("facts") or [] if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        fact = re.sub(r"\s+", " ", str(item.get("text") or "")).strip(" .")
        kind = str(item.get("kind") or "profile")
        if not 4 <= len(fact) <= 200 or _SENSITIVE.search(fact):
            continue
        facts.append(
            {
                "text": fact[:1].upper() + fact[1:] + ".",
                "kind": kind if kind in {"preference", "constraint", "profile"} else "profile",
            }
        )
    return facts[:_MAX_FACTS]


async def extract_facts(model, message: str) -> list[dict[str, str]] | None:
    """Facts from one message, or None when the model could not be asked."""
    if not worth_extracting(message):
        return []
    try:
        response = await model.generate(
            InferenceRequest(
                messages=[
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": message.strip()[:2000]},
                ],
                temperature=0.0,
                max_output_tokens=300,
                json_mode=True,
            )
        )
    except MODEL_CALL_FAILURES as exc:  # background work: never surfaced to the user
        log_event("memory_extraction_unavailable", details={"error_type": type(exc).__name__})
        return None
    return parse_facts(getattr(response, "text", None))


def save_facts(
    memory_service, user_id: int, facts: list[dict[str, Any]], source_message_id=None
) -> list[int]:
    saved = []
    for fact in facts:
        importance = 0.8 if fact.get("kind") in {"constraint", "profile"} else 0.7
        row = memory_service.save_memory(
            user_id,
            fact["text"],
            "preference" if fact.get("kind") == "preference" else "fact",
            importance,
            source_message_id,
        )
        if row is not None:
            saved.append(row.id)
    if saved:
        log_event("memory_facts_saved", details={"count": len(saved)})
    return saved
