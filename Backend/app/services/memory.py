from __future__ import annotations

from groq import AsyncGroq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Conversation, Message
from app.observability import logger

SUMMARY_MESSAGE_THRESHOLD = 8
SUMMARY_INTERVAL = 4
SUMMARY_RAW_WINDOW = 12

# Conversations are about anything, not only films: the summary keeps what a
# later turn may depend on, whatever the topic.
SUMMARY_SYSTEM_PROMPT = (
    "Update a dense, factual rolling summary of an assistant conversation. Keep what later turns may "
    "depend on: facts the user stated about themselves and their constraints (names, who they watch with, "
    "language or format needs), explicit preferences, decisions made, open tasks or questions, drafts or "
    "lists being worked on and their current state, films and people discussed, and any plans. Keep the "
    "user's own wording for constraints. Distinguish stated facts from guesses and preserve uncertainty. "
    "Merge the prior summary with the new messages; drop what was superseded. Plain text, at most 180 words."
)


async def maybe_compact_conversation(conversation_id: str | int, db: Session, client: AsyncGroq) -> None:
    """Refresh the rolling summary when an eligible conversation grows."""
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.is_deleted.is_(False),
        )
    )
    if conversation is None:
        return

    message_count = (
        db.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id,
                Message.is_deleted.is_(False),
            )
        )
        or 0
    )
    if message_count <= SUMMARY_MESSAGE_THRESHOLD or message_count % SUMMARY_INTERVAL != 0:
        return

    older_messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.is_deleted.is_(False),
            Message.role.in_(("user", "assistant")),
            Message.status.in_(("complete", "stopped")),
            (
                Message.id > conversation.summary_through_message_id
                if conversation.summary_through_message_id is not None
                else True
            ),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(max(0, message_count - SUMMARY_RAW_WINDOW))
    ).all()
    if not older_messages:
        return

    history = "\n".join(f"{item.role}: {item.content}" for item in older_messages)
    prior_summary = conversation.summary or ""
    response = await client.chat.completions.create(
        model=settings.groq_summary_model,
        temperature=0.2,
        max_tokens=500,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Prior summary:\n{prior_summary or '(none)'}\n\n"
                    f"Older conversation messages:\n{history}"
                ),
            },
        ],
    )
    updated_summary = (response.choices[0].message.content or "").strip()
    if not updated_summary:
        logger.warning("conversation_compaction_empty_summary conversation_id=%s", conversation_id)
        return
    conversation.summary = updated_summary
    conversation.summary_through_message_id = older_messages[-1].id
    db.commit()
