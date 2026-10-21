from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import Conversation, Memory, Message, MovieReference, utcnow


def purge_expired_conversations(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=30)
    expired_ids = db.scalars(
        select(Conversation.id).where(
            Conversation.is_deleted.is_(True),
            Conversation.deleted_at <= cutoff,
        )
    ).all()
    if not expired_ids:
        return 0

    message_ids = select(Message.id).where(Message.conversation_id.in_(expired_ids))
    db.execute(delete(Memory).where(Memory.source_message_id.in_(message_ids)))
    db.execute(delete(MovieReference).where(MovieReference.conversation_id.in_(expired_ids)))
    db.execute(delete(Message).where(Message.conversation_id.in_(expired_ids)))
    db.execute(delete(Conversation).where(Conversation.id.in_(expired_ids)))
    db.commit()
    return len(expired_ids)
