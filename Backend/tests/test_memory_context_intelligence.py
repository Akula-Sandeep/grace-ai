from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, Conversation, Memory, Message, User
from app.memory import MemoryService


def test_relevant_history_can_be_scoped_to_current_conversation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/memory_scope.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="memory-user", password_hash="x")
    db.add(user)
    db.commit()
    first = Conversation(user_id=user.id, title="First")
    second = Conversation(user_id=user.id, title="Second")
    db.add_all([first, second])
    db.commit()
    db.add_all(
        [
            Message(
                user_id=user.id,
                conversation_id=first.id,
                role="user",
                content="The first film has a memorable lighthouse scene.",
            ),
            Message(
                user_id=user.id,
                conversation_id=second.id,
                role="user",
                content="This conversation is about a different topic.",
            ),
        ]
    )
    db.commit()

    rows = MemoryService(db).relevant_history(
        user.id,
        "tell me about the lighthouse scene",
        conversation_id=second.id,
    )

    assert rows == []


def test_context_memory_retrieval_can_avoid_write_side_effects(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/memory_touch.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="touch-user", password_hash="x")
    db.add(user)
    db.commit()
    memory = Memory(
        user_id=user.id,
        content="User prefers thoughtful science fiction.",
        importance=0.8,
        last_used_at=None,
    )
    db.add(memory)
    db.commit()

    results = MemoryService(db).memories(
        user.id,
        "thoughtful science fiction",
        touch=False,
    )
    db.expire_all()

    assert results
    assert db.get(Memory, memory.id).last_used_at is None
