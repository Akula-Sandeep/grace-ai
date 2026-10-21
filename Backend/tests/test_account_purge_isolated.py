from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.database import AuditLog, Base, Conversation, User
from app.services import account_lifecycle


def test_deleted_account_purge_removes_children_and_anonymizes_audit(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'purge.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(account_lifecycle, "SessionLocal", factory)

    deleted_at = datetime.now(UTC) - timedelta(days=15)
    with factory() as db:
        user = User(
            username="purge-user",
            email="purge@example.test",
            password_hash="x",
            status="deleted",
            deleted_at=deleted_at,
        )
        db.add(user)
        db.flush()
        db.add(Conversation(user_id=user.id, title="private history"))
        db.add(
            AuditLog(
                user_id=user.id,
                event_type="account.deleted",
                metadata_json='{"reason":"user_request"}',
            )
        )
        db.commit()
        user_id = user.id

    assert account_lifecycle.purge_deleted_accounts(now=datetime.now(UTC)) == 1

    with factory() as db:
        assert db.get(User, user_id) is None
        assert db.query(Conversation).filter_by(user_id=user_id).count() == 0
        audit = db.query(AuditLog).filter_by(user_id=None).one()
        assert '"user_hash":' in audit.metadata_json
        import hashlib

        expected_hash = hashlib.sha256(f"grace-user:{user_id}".encode()).hexdigest()
        assert expected_hash in audit.metadata_json

    engine.dispose()
