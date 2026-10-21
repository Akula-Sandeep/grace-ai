from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.database import Base, Conversation, Memory, Message, User, utcnow
from app.services.conversation_purge import purge_expired_conversations


@pytest.fixture
def conversation_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def test_delete_conversation_soft_deletes_only_owned_conversation_and_messages(
    conversation_db,
):
    with conversation_db() as db:
        owner = User(username="owner", password_hash="hash")
        other_user = User(username="other", password_hash="hash")
        db.add_all([owner, other_user])
        db.flush()
        owned = Conversation(user_id=owner.id, title="Owned")
        other = Conversation(user_id=other_user.id, title="Other")
        db.add_all([owned, other])
        db.flush()
        db.add_all(
            [
                Message(
                    conversation_id=owned.id,
                    user_id=owner.id,
                    role="user",
                    content="Hello",
                ),
                Message(
                    conversation_id=other.id,
                    user_id=other_user.id,
                    role="user",
                    content="Hi",
                ),
            ]
        )
        owned_id, other_id, owner_id = owned.id, other.id, owner.id
        db.commit()

    result = main.delete_conversation(owned_id, SimpleNamespace(id=owner_id))
    assert result["ok"] is True
    assert result["id"] == owned_id
    assert result["undo_until"]

    with conversation_db() as db:
        deleted_conversation = db.get(Conversation, owned_id)
        deleted_message = db.scalar(select(Message).where(Message.conversation_id == owned_id))
        assert deleted_conversation is not None
        assert deleted_conversation.is_deleted is True
        assert deleted_conversation.deleted_at is not None
        assert deleted_message is not None
        assert deleted_message.is_deleted is True
        assert deleted_message.deleted_at is not None
        assert db.get(Conversation, other_id) is not None

    with pytest.raises(HTTPException) as error:
        main.delete_conversation(other_id, SimpleNamespace(id=owner_id))
    assert error.value.status_code == 404


def test_hard_delete_conversation_cascades_orm_messages(conversation_db):
    with conversation_db() as db:
        user = User(username="cascade-user", password_hash="hash")
        db.add(user)
        db.flush()
        conversation = Conversation(user_id=user.id)
        db.add(conversation)
        db.flush()
        db.add(
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="user",
                content="Delete with the conversation.",
            )
        )
        conversation_id = conversation.id
        db.commit()

        db.delete(conversation)
        db.commit()

        assert db.scalar(select(Message).where(Message.conversation_id == conversation_id)) is None
        assert db.get(Conversation, conversation_id) is None

    index_columns = {
        tuple(column.name for column in index.columns)
        for index in Message.__table__.indexes
        if index.name == "ix_messages_convo_deleted_created"
    }
    assert index_columns == {("conversation_id", "is_deleted", "created_at")}


def test_rename_conversation_requires_ownership_and_persists_trimmed_title(
    conversation_db,
):
    with conversation_db() as db:
        owner = User(username="owner", password_hash="hash")
        other_user = User(username="other", password_hash="hash")
        db.add_all([owner, other_user])
        db.flush()
        conversation = Conversation(user_id=owner.id, title="Old title")
        db.add(conversation)
        db.flush()
        conversation_id, owner_id, other_id = conversation.id, owner.id, other_user.id
        db.commit()

    result = main.rename_conversation(
        conversation_id,
        main.ConversationRenameIn(title="  New title  "),
        SimpleNamespace(id=owner_id),
    )
    assert result == {"id": conversation_id, "title": "New title"}

    with pytest.raises(HTTPException) as error:
        main.rename_conversation(
            conversation_id,
            main.ConversationRenameIn(title="Unauthorized"),
            SimpleNamespace(id=other_id),
        )
    assert error.value.status_code == 404


def test_restore_conversation_undoes_soft_delete(conversation_db):
    with conversation_db() as db:
        user = User(username="restore-owner", password_hash="hash")
        db.add(user)
        db.flush()
        conversation = Conversation(user_id=user.id, title="Restore me")
        db.add(conversation)
        db.flush()
        message = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="A restorable message",
        )
        db.add(message)
        db.commit()
        conversation_id, user_id = conversation.id, user.id
    main.delete_conversation(conversation_id, SimpleNamespace(id=user_id))

    assert main.restore_conversation(conversation_id, SimpleNamespace(id=user_id)) == {
        "ok": True,
        "id": conversation_id,
    }
    with conversation_db() as db:
        assert db.get(Conversation, conversation_id).is_deleted is False
        assert (
            db.scalar(select(Message).where(Message.conversation_id == conversation_id)).is_deleted is False
        )


def test_purge_expired_deleted_conversations_also_removes_derived_memories(
    conversation_db,
):
    with conversation_db() as db:
        user = User(username="purge-owner", password_hash="hash")
        db.add(user)
        db.flush()
        conversation = Conversation(
            user_id=user.id,
            title="Expired",
            is_deleted=True,
            deleted_at=utcnow() - timedelta(days=31),
        )
        db.add(conversation)
        db.flush()
        message = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="Old conversation memory",
        )
        db.add(message)
        db.flush()
        memory = Memory(
            user_id=user.id,
            content="Derived from the old turn",
            source_message_id=message.id,
        )
        conversation_id = conversation.id
        db.add(memory)
        db.commit()

        assert purge_expired_conversations(db) == 1
        assert db.get(Conversation, conversation_id) is None
        assert db.scalar(select(Memory).where(Memory.content == "Derived from the old turn")) is None
