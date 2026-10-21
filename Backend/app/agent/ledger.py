"""Exactly-once side effects for chat actions.

"Try again", regenerate, a dropped connection or a double send repeat the same
request. Adding to a watchlist is naturally idempotent, but a second reminder,
or "removed" turning into "wasn't on your watchlist", is not. Every action a
turn performs is recorded against the request's stable key; a repeat of the
same request finds the record and reports the original outcome instead of
acting again.

Signed-in users: the ``chat_actions`` table (unique per user, request and
action). Guests: the shared session store, for as long as the guest session.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.agent.turn import current_turn
from app.config import settings
from app.observability import logger
from app.shared_state import shared_state

_GUEST_TTL = 24 * 60 * 60


def action_key(intent: str, tmdb_id: int | None, detail: str | None = None) -> str:
    key = f"{intent}:{tmdb_id or '-'}"
    if detail:
        key += ":" + " ".join(detail.casefold().split())[:60]
    return key[:160]


def _guest_key(guest_session_id: str, request_key: str, key: str) -> str:
    return f"guest-action:{guest_session_id}:{request_key}:{key}"


async def recall(key: str, *, db_factory=None) -> dict[str, Any] | None:
    """The recorded outcome of this action for this request, if it already happened."""
    turn = current_turn()
    if turn is None or not turn.request_key:
        return None
    if turn.user_id is not None and db_factory is not None:
        from app.database import ChatAction, run_db

        def load(db):
            row = db.scalar(
                select(ChatAction.outcome).where(
                    ChatAction.user_id == turn.user_id,
                    ChatAction.request_key == turn.request_key,
                    ChatAction.action_key == key,
                )
            )
            return row if isinstance(row, dict) else None

        try:
            return await run_db(load, session_factory=db_factory)
        except SQLAlchemyError as exc:
            logger.warning("action_ledger_read_failed key=%s error=%r", key, exc)
            return None
    if turn.guest_session_id:
        value = await shared_state.cache_get(
            _guest_key(turn.guest_session_id, turn.request_key, key), _GUEST_TTL
        )
        return value if isinstance(value, dict) else None
    return None


async def remember(key: str, outcome: dict[str, Any], *, db_factory=None) -> None:
    """Record that this request performed this action (first writer wins)."""
    turn = current_turn()
    if turn is None or not turn.request_key:
        return
    turn.actions.append({"action": key.split(":", 1)[0], "tmdb_id": outcome.get("tmdb_id"), "key": key})
    if turn.user_id is not None and db_factory is not None:
        from app.database import ChatAction, run_db

        def save(db):
            db.add(
                ChatAction(
                    user_id=turn.user_id,
                    request_key=turn.request_key,
                    action_key=key,
                    outcome=outcome,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # a concurrent repeat of the same request recorded it first

        try:
            await run_db(save, session_factory=db_factory)
        except SQLAlchemyError as exc:
            logger.warning("action_ledger_write_failed key=%s error=%r", key, exc)
        return
    if turn.guest_session_id:
        await shared_state.cache_set(
            _guest_key(turn.guest_session_id, turn.request_key, key),
            outcome,
            settings.cache_max_entries,
            ttl=_GUEST_TTL,
        )
