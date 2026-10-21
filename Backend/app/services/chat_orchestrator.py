from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.prompts import GRACE_SYSTEM_PROMPT
from app.database import Conversation, Message

RAW_MESSAGE_WINDOW = 8


def assemble_chat_payload(
    conversation_id: str | int,
    current_message: str,
    db: Session,
    *,
    current_message_id: int | None = None,
    summary: str | None = None,
    window: int = RAW_MESSAGE_WINDOW,
) -> list[dict[str, str]]:
    """Build a stable-prefix prompt with a summary and a bounded raw turn window.

    ``window`` is how many recent messages of this branch to include; the
    model-led path asks for more and then fits them to its token budget.
    """
    window = max(2, int(window))
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.is_deleted.is_(False),
        )
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": GRACE_SYSTEM_PROMPT}]
    if summary is None:
        summary = getattr(conversation, "summary", None)
    if conversation and summary:
        messages.append(
            {
                "role": "system",
                "content": f"[Session Context Summary: {summary}]",
            }
        )

    message_query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.is_deleted.is_(False),
        Message.role.in_(("user", "assistant")),
        Message.status.in_(("complete", "stopped")),
    )
    history_limit = window
    if current_message_id is not None:
        from app.database import branch_lineage_ids

        current = db.get(Message, current_message_id)
        # The branch above this message, nearest first, in one bounded query; only
        # the last few turns are used, so the walk stops well before the root.
        lineage_ids = branch_lineage_ids(
            db,
            conversation_id,
            current.parent_id if current else None,
            max_depth=window * 4,
        )
        if lineage_ids or (current is not None and current.client_message_id is not None):
            message_query = message_query.where(Message.id.in_(lineage_ids))
        else:
            message_query = message_query.where(Message.id != current_message_id)
        history_limit = window - 1
    recent = db.scalars(
        message_query.order_by(Message.created_at.desc(), Message.id.desc()).limit(history_limit)
    ).all()
    recent = list(reversed(recent))
    if (
        current_message_id is None
        and recent
        and recent[-1].role == "user"
        and recent[-1].content.strip() == current_message.strip()
    ):
        recent.pop()
    messages.extend({"role": item.role, "content": item.content} for item in recent[-(window - 1) :])
    messages.append({"role": "user", "content": current_message})
    return messages
