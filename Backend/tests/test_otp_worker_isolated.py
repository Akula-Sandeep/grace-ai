import sys
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Avoid importing the absent SMTP package; the worker contract is tested with a sender stub.
if "aiosmtplib" not in sys.modules:
    smtp = types.ModuleType("aiosmtplib")

    async def _send(*_args, **_kwargs):
        return None

    smtp.send = _send
    sys.modules["aiosmtplib"] = smtp

from app import database
from app.database import Base, OTPDeliveryJob
from app.jobs import process_background
from app.services.otp_delivery_service import encrypt_code


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'otp-worker.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(process_background, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _seed(factory):
    with factory() as db:
        job = OTPDeliveryJob(
            email="otp@example.test",
            code_ciphertext=encrypt_code("123456"),
            purpose="verification",
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC) - timedelta(seconds=1),
            created_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def test_otp_worker_marks_successful_delivery(worker_db, monkeypatch):
    job_id = _seed(worker_db)
    sent = []

    def sender(email, code, purpose="verification"):
        sent.append((email, code, purpose))

    monkeypatch.setattr(process_background, "send_otp_email", sender)

    assert process_background.process_otp_jobs_once("worker-a") == 1
    with worker_db() as db:
        job = db.get(OTPDeliveryJob, job_id)
        assert job.status == "sent"
        assert job.sent_at is not None
        assert job.attempts == 1
        assert job.locked_at is None
    assert sent == [("otp@example.test", "123456", "verification")]


def test_otp_worker_requeues_failed_delivery_with_backoff(worker_db, monkeypatch):
    job_id = _seed(worker_db)

    def sender(_email, _code, _purpose="verification"):
        raise RuntimeError("smtp outage")

    monkeypatch.setattr(process_background, "send_otp_email", sender)

    assert process_background.process_otp_jobs_once("worker-a") == 0
    with worker_db() as db:
        job = db.get(OTPDeliveryJob, job_id)
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.locked_at is None
        assert job.last_error == "RuntimeError"
        available_at = (
            job.available_at.replace(tzinfo=UTC) if job.available_at.tzinfo is None else job.available_at
        )
        assert available_at > datetime.now(UTC)
