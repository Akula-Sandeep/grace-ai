import json
import zipfile
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.database import Base, Conversation, ExportJob, Message, User
from app.services import export_service


def _factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'export-full.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(export_service, "SessionLocal", factory)
    monkeypatch.setattr(export_service.settings, "export_dir", str(tmp_path / "exports"))
    return engine, factory


def test_export_job_generates_bounded_streaming_zip_and_records_metadata(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path, monkeypatch)
    with factory() as db:
        user = User(username="export-user", email="export@example.test", password_hash="x")
        db.add(user)
        db.flush()
        conversation = Conversation(user_id=user.id, title="My chat")
        db.add(conversation)
        db.flush()
        for idx in range(3):
            db.add(
                Message(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    role="user",
                    content=f"message {idx}",
                    status="complete",
                )
            )
        job = ExportJob(user_id=user.id, status="pending", created_at=datetime.now(UTC))
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    export_service.generate_export_job(job_id, worker_id="test-worker")

    with factory() as db:
        job = db.get(ExportJob, job_id)
        assert job.status == "ready"
        assert job.progress == 100
        assert job.artifact_path
        assert job.artifact_size > 0
        assert job.download_token_hash
        artifact_path = job.artifact_path

    with zipfile.ZipFile(artifact_path) as archive:
        names = set(archive.namelist())
        assert "account.json" in names
        assert "conversations.ndjson" in names
        assert "messages.ndjson" in names
        messages = [json.loads(line) for line in archive.read("messages.ndjson").decode().splitlines()]
        assert len(messages) == 3

    engine.dispose()


def test_export_job_enforces_row_limit_and_marks_failed(tmp_path, monkeypatch):
    engine, factory = _factory(tmp_path, monkeypatch)
    monkeypatch.setattr(export_service, "MAX_ROWS", 2)
    with factory() as db:
        user = User(username="limit-user", email="limit@example.test", password_hash="x")
        db.add(user)
        db.flush()
        conversation = Conversation(user_id=user.id, title="Limit chat")
        db.add(conversation)
        db.flush()
        for idx in range(3):
            db.add(
                Message(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    role="user",
                    content=str(idx),
                    status="complete",
                )
            )
        job = ExportJob(user_id=user.id, status="pending", created_at=datetime.now(UTC))
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    try:
        export_service.generate_export_job(job_id, worker_id="test-worker")
    except ValueError as exc:
        assert str(exc) == "export_row_limit_exceeded"

    with factory() as db:
        job = db.get(ExportJob, job_id)
        assert job.status == "failed"
        assert job.last_error == "ValueError"
        assert job.locked_at is None

    engine.dispose()
