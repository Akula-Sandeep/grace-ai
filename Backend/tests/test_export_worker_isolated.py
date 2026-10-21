import sys
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if "aiosmtplib" not in sys.modules:
    smtp = types.ModuleType("aiosmtplib")

    async def _send(*_args, **_kwargs):
        return None

    smtp.send = _send
    sys.modules["aiosmtplib"] = smtp

from app import database
from app.database import Base, ExportJob, User
from app.jobs import process_background


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export-worker.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(process_background, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _seed(factory, *, status="running", locked_at=None, attempts=0):
    with factory() as db:
        user = User(
            username=f"export-worker-{attempts}",
            email=f"export-worker-{attempts}@example.test",
            password_hash="x",
        )
        db.add(user)
        db.flush()
        job = ExportJob(
            user_id=user.id,
            status=status,
            stage="queued",
            progress=0,
            attempts=attempts,
            locked_at=locked_at,
            created_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def test_export_worker_counts_stale_claim_as_attempt(worker_db, monkeypatch):
    stale = datetime.now(UTC) - timedelta(seconds=301)
    job_id = _seed(worker_db, locked_at=stale, attempts=0)
    monkeypatch.setattr(process_background, "generate_export_job", lambda *_args, **_kwargs: None)

    assert process_background.process_export_jobs_once("export-worker") == 1
    with worker_db() as db:
        job = db.get(ExportJob, job_id)
        assert job.status == "running"
        assert job.attempts == 1
        assert job.locked_by == "export-worker"


def test_export_worker_marks_terminal_stale_job_failed_after_five_attempts(worker_db, monkeypatch):
    stale = datetime.now(UTC) - timedelta(seconds=301)
    job_id = _seed(worker_db, locked_at=stale, attempts=5)
    monkeypatch.setattr(process_background, "generate_export_job", lambda *_args, **_kwargs: None)

    assert process_background.process_export_jobs_once("export-worker") == 0
    with worker_db() as db:
        job = db.get(ExportJob, job_id)
        assert job.attempts == 5
        assert job.status == "failed"
        assert job.last_error == "max_attempts_exceeded"
        assert job.locked_at is None
