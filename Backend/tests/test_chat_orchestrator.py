from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base, Conversation, Message, User
from app.services.chat_orchestrator import assemble_chat_payload


def test_payload_keeps_recent_history_chronological_and_current_turn_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(username="history-user", password_hash="test")
            db.add(user)
            db.flush()
            conversation = Conversation(user_id=user.id, summary="Interstellar was discussed.")
            db.add(conversation)
            db.flush()

            start = datetime.now(UTC)
            messages = [
                Message(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content="Repeat this question." if index == 8 else f"Turn {index}",
                    created_at=start + timedelta(seconds=index),
                )
                for index in range(9)
            ]
            db.add_all(messages)
            db.flush()
            current = Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user",
                content="Repeat this question.",
                created_at=start + timedelta(seconds=9),
            )
            db.add(current)
            db.commit()

            payload = assemble_chat_payload(
                conversation.id,
                current.content,
                db,
                current_message_id=current.id,
            )

        assert payload[0]["role"] == "system"
        assert payload[1] == {
            "role": "system",
            "content": "[Session Context Summary: Interstellar was discussed.]",
        }
        assert payload[2:-1] == [
            {
                "role": "assistant" if index % 2 else "user",
                "content": "Repeat this question." if index == 8 else f"Turn {index}",
            }
            for index in range(2, 9)
        ]
        assert payload[-1] == {"role": "user", "content": "Repeat this question."}
        assert sum(message["content"] == "Repeat this question." for message in payload) == 2
    finally:
        engine.dispose()
