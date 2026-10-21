"""Turn answers users marked "not helpful" into candidate eval cases.

Each thumbs-down becomes one JSON line with the branch's user turns (so the
conversation can be replayed), the answer that was rejected, the reason and note
the user gave, and how the answer was produced. A person then turns the useful
ones into scenarios with expectations (``live_scenarios.json`` or a regression
test): the reason says what to check.

    docker compose exec backend python -m app.evals.feedback_cases --out /tmp/feedback-cases.jsonl
    docker compose cp backend:/tmp/feedback-cases.jsonl ./eval-reports/

These files contain what users wrote. Keep them out of the repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.database import Message, branch_lineage_ids

# What a reason most likely calls for, as a starting point for the expectation.
REASON_HINTS = {
    "wrong_film": "Check the film the answer is about (title resolution, year, focus).",
    "wrong_facts": "Check each fact against the movie data; the answer may have used model memory.",
    "ignored_request": "Check that every constraint in the user's message is honoured.",
    "too_long": "Check length and whether the answer stayed on the question.",
    "not_useful": "Read the answer against the question; decide the expected behaviour.",
    "other": "Read the user's note.",
}

_TURN_CHARS = 2000


def _clip(text: str | None, limit: int = _TURN_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def collect_cases(db, *, since: datetime | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Candidate cases for every thumbs-down answer (newest first)."""
    query = select(Message).where(
        Message.role == "assistant",
        Message.feedback == "down",
        Message.is_deleted.is_(False),
    )
    if since is not None:
        query = query.where(Message.created_at >= since)
    answers = db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)).all()
    cases = []
    for answer in answers:
        lineage = branch_lineage_ids(db, answer.conversation_id, answer.parent_id)
        rows = (
            {m.id: m for m in db.scalars(select(Message).where(Message.id.in_(lineage))).all()}
            if lineage
            else {}
        )
        history = [rows[i] for i in reversed(lineage) if i in rows and not rows[i].is_deleted]
        user_turns = [_clip(m.content) for m in history if m.role == "user"]
        state = answer.turn_state if isinstance(answer.turn_state, dict) else {}
        cases.append(
            {
                "id": answer.uuid_id,
                "conversation_id": answer.conversation_id,
                "created_at": answer.created_at.isoformat() if answer.created_at else None,
                "reason": answer.feedback_reason,
                "note": answer.feedback_note,
                "hint": REASON_HINTS.get(answer.feedback_reason or "", REASON_HINTS["not_useful"]),
                "turns": user_turns,
                "rejected_answer": _clip(answer.content, 6000),
                "produced_by": {
                    "model": answer.model,
                    "provider": answer.provider,
                    "status": answer.status,
                    "error_code": answer.error_code,
                    "latency_ms": answer.latency_ms,
                },
                "focus": state.get("focus"),
            }
        )
    return cases


def main(argv: list[str] | None = None) -> int:
    from app.database import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="JSONL file to write")
    parser.add_argument("--days", type=int, default=0, help="only the last N days (0: all)")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)
    since = datetime.now(UTC) - timedelta(days=args.days) if args.days > 0 else None
    with SessionLocal() as db:
        cases = collect_cases(db, since=since, limit=args.limit)
    with open(args.out, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    sys.stdout.write(f"{len(cases)} candidate case(s) written to {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
