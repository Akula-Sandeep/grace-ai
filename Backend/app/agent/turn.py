"""The record of one chat turn, shared by every stage that handles it.

Created by the chat endpoint (or by the agent when called directly) and bound
for the whole turn. Stages read what they need from it (who is asking, the
stable id of the request) and write what the endpoint must persist or report
afterwards: the working state for the next turn, the actions performed, and
how the answer was produced (for the turn record in logs and Settings → Help).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnContext:
    # Stable id of the user's request: the persisted user message for a signed-in
    # user, the client's message id for a guest. A retry or regeneration of the
    # same request keeps it, so side effects are never repeated (see ledger.py).
    request_key: str | None = None
    user_id: int | None = None
    guest_session_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    # Written during the turn
    state_out: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    intent: str | None = None
    intent_source: str | None = None
    handler: str | None = None
    answer_source: str | None = None  # model | deterministic | digest | gemini | error
    model: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    stages: dict[str, float] = field(default_factory=dict)
    # Server-side time from the request to the first answer text sent to the browser.
    first_token_ms: float | None = None

    def note_stage(self, name: str, started: float) -> None:
        self.stages[name] = round((time.monotonic() - started) * 1000, 1)

    def note_first_token(self) -> None:
        if self.first_token_ms is None:
            self.first_token_ms = round((time.monotonic() - self.started_at) * 1000, 1)

    def fallback(self, reason: str) -> None:
        if reason not in self.fallbacks and len(self.fallbacks) < 12:
            self.fallbacks.append(reason)

    def summary(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "intent_source": self.intent_source,
            "handler": self.handler,
            "answer_source": self.answer_source,
            "model": self.model,
            "fallbacks": self.fallbacks,
            "actions": [{"action": a.get("action"), "tmdb_id": a.get("tmdb_id")} for a in self.actions][:8],
            "stages_ms": self.stages,
            "first_token_ms": self.first_token_ms,
            "total_ms": round((time.monotonic() - self.started_at) * 1000, 1),
        }


_TURN: ContextVar[TurnContext | None] = ContextVar("grace_turn", default=None)


def current_turn() -> TurnContext | None:
    return _TURN.get()


@contextmanager
def bind_turn(turn: TurnContext) -> Iterator[TurnContext]:
    token = _TURN.set(turn)
    try:
        yield turn
    finally:
        _TURN.reset(token)


def ensure_turn(**fields: Any) -> TurnContext:
    """The bound turn, or a new one bound for the caller (direct agent use, tests)."""
    turn = _TURN.get()
    if turn is None:
        turn = TurnContext(**fields)
        _TURN.set(turn)
    else:
        for key, value in fields.items():
            if value is not None and getattr(turn, key, None) is None:
                setattr(turn, key, value)
    return turn
