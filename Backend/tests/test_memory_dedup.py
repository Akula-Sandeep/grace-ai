import types

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, Memory, User
from app.memory import MemoryService, memory_content_hash


def test_memory_dedup_uses_canonical_hash(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/memory_dedup.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        user = User(username="memory-hash-user", password_hash="x")
        db.add(user)
        db.commit()

        service = MemoryService(db)
        first = service.save_memory(user.id, "User likes   thoughtful science fiction.", importance=0.4)
        second = service.save_memory(user.id, " user LIKES thoughtful science fiction. ", importance=0.9)

        rows = db.scalars(select(Memory).where(Memory.user_id == user.id)).all()
        assert len(rows) == 1
        assert first.id == second.id == rows[0].id
        assert rows[0].content_hash == memory_content_hash(rows[0].content)
        assert rows[0].importance == 0.9
    finally:
        db.close()
        engine.dispose()


def test_memory_dedup_handles_concurrent_unique_race(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/memory_race.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        user = User(username="memory-race-user", password_hash="x")
        db.add(user)
        db.commit()
        service = MemoryService(db)

        original_commit = db.commit
        raced = {"done": False}
        other = Session()

        def commit_with_race():
            if not raced["done"]:
                raced["done"] = True
                other.add(
                    Memory(
                        user_id=user.id,
                        content="A concise memory",
                        importance=0.5,
                    )
                )
                other.commit()
            return original_commit()

        db.commit = types.MethodType(lambda _self: commit_with_race(), db)

        result = service.save_memory(user.id, "  A   concise memory  ", importance=0.95)
        db.commit = original_commit
        other.close()

        rows = db.scalars(select(Memory).where(Memory.user_id == user.id)).all()
        assert len(rows) == 1
        assert result.id == rows[0].id
        assert rows[0].importance == 0.95
    finally:
        try:
            other.close()
        except Exception:
            pass
        db.close()
        engine.dispose()


def test_memory_model_declares_durable_user_hash_uniqueness():
    table = Memory.__table__
    constraint = next(
        c for c in table.constraints if getattr(c, "name", None) == "uq_memories_user_content_hash"
    )
    assert [column.name for column in constraint.columns] == ["user_id", "content_hash"]
