"""Model-graded evaluation for the live eval (LLM-as-judge).

Each scenario is graded by Gemini, never the model that answered (Grace's chat
model is gpt-oss-120b), from the full transcript: what the user said, what Grace
replied, and what the app itself recorded for each turn (verified list changes,
errors). The judge checks every turn's criteria (met or not, with a reason) and
scores the scenario's rubric 0 (fails), 1 (partly) or 2 (fully).

This replaced regex expect/reject patterns (owner, 2026-10-16), which failed
correct answers ("doesn't specify" against /not/) and passed wrong ones.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

_INSTRUCTIONS = """You grade transcripts of Grace, an AI assistant whose speciality is film.

You get one conversation: each turn has what the user said, Grace's reply, and what the
app recorded for that turn (changes it verified in the user's lists, errors). The app's
record is the truth about what changed; Grace's words are not.

For every turn, decide for each criterion whether the reply meets it, with a one-sentence
reason. Judge the meaning, not the wording: any phrasing that says the same thing counts.
Use your own knowledge of films and the world for facts; when a criterion states a fact,
that fact is correct. Then score each rubric item for the whole conversation: 0 = fails,
1 = partly, 2 = fully, with a one-sentence reason.

Be strict about facts, invented details, claims of changes the app did not record, and
the format the user asked for. Be fair about style: any natural, correct reply passes."""


class CriterionGrade(BaseModel):
    criterion: str
    met: bool
    reason: str


class TurnGrade(BaseModel):
    turn: int = Field(description="1-based turn number")
    criteria: list[CriterionGrade]


class RubricGrade(BaseModel):
    item: str
    score: int = Field(ge=0, le=2)
    reason: str


class Grade(BaseModel):
    turns: list[TurnGrade]
    rubric: list[RubricGrade]


def transcript(scenario: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for number, (spec, turn) in enumerate(zip(scenario["turns"], turns, strict=False), start=1):
        record = [f"verified change: {action}" for action in turn.get("actions") or []]
        record += [f"error shown to the user: {error}" for error in turn.get("errors") or []]
        criteria = spec.get("criteria") or []
        parts.append(
            f"## Turn {number}\n"
            f"User: {turn['say']}\n"
            f"Grace: {turn.get('answer') or '(no reply)'}\n"
            f"App record: {'; '.join(record) if record else 'no list changes, no errors'}\n"
            "Criteria:\n" + ("\n".join(f"- {c}" for c in criteria) if criteria else "- (none for this turn)")
        )
    rubric = "\n".join(f"- {item}" for item in scenario.get("rubric") or [])
    return "\n\n".join(parts) + f"\n\n## Rubric for the whole conversation\n{rubric}"


async def grade(scenario: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    """{"turns": [...], "rubric": [...], "judge_model": ...} or {"error": ...}; never raises."""
    from app.config import settings

    if not settings.gemini_api_key:
        return {"error": "no judge: GEMINI_API_KEY is not set"}
    from google import genai
    from google.genai import types

    from app.services.movie_data_fallback import _gemini_generate, gemini_model_used

    client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=60_000))
    try:

        async def ask(model: str):
            return await client.aio.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [{"text": transcript(scenario, turns)}]}],
                config=types.GenerateContentConfig(
                    system_instruction=_INSTRUCTIONS,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=Grade,
                ),
            )

        try:
            response = await _gemini_generate(ask, label="eval_judge", kind="judge")
            graded = Grade.model_validate(json.loads(response.text or "{}"))
        except Exception as exc:
            return {"error": f"the judge could not grade this scenario: {type(exc).__name__}"}
        return {**graded.model_dump(), "judge_model": gemini_model_used()}
    finally:
        await client.aio.aclose()


def outcome(judged: dict[str, Any], scenario: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """(passed, problems). None when the scenario could not be graded.

    Passes when every criterion is met and no rubric item scores 0.
    """
    if judged.get("error"):
        return None, [judged["error"]]
    problems = [
        f"turn {turn['turn']}: {c['criterion']} — {c['reason']}"
        for turn in judged.get("turns") or []
        for c in turn.get("criteria") or []
        if not c.get("met")
    ]
    problems += [
        f"rubric: {r['item']} scored 0 — {r['reason']}"
        for r in judged.get("rubric") or []
        if r.get("score") == 0
    ]
    expected = sum(len(t.get("criteria") or []) for t in scenario["turns"])
    graded = sum(len(t.get("criteria") or []) for t in judged.get("turns") or [])
    if graded < expected:
        problems.append(f"the judge graded {graded} of {expected} criteria")
    return not problems, problems
