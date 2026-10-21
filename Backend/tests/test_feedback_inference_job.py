from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event

from app.database import Base, Feedback, FeedbackInferenceJob, Movie, SessionLocal, User
from app.jobs.process_feedback_inference import run_pending_feedback_jobs_once
from app.movie_state import MovieStateService


@pytest.fixture
def isolated_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback_jobs.db'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    original_bind = SessionLocal.kw["bind"]
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        SessionLocal.configure(bind=original_bind)
        engine.dispose()


def seed_feedback():
    with SessionLocal() as db:
        user = User(
            username="feedback-job-user",
            email="feedback-job@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        movie = Movie(tmdb_id=101, title="Example Movie")
        db.add_all([user, movie])
        db.flush()
        feedback = Feedback(user_id=user.id, movie_id=movie.id, feedback_type="product", text="Great")
        db.add(feedback)
        db.flush()
        job = FeedbackInferenceJob(feedback_id=feedback.id, user_id=user.id, available_at=datetime.now(UTC))
        db.add(job)
        db.commit()
        return feedback.id, job.id, user.id


@pytest.mark.asyncio
async def test_feedback_request_commits_job_with_feedback(isolated_database):
    with SessionLocal() as db:
        user = User(
            username="feedback-request-user",
            email="feedback-request@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        movie = Movie(tmdb_id=202, title="Feedback Movie")
        db.add_all([user, movie])
        db.commit()
        user_id = user.id
        movie_id = movie.id

        service = MovieStateService(db, object())
        service.ensure_movie = AsyncMock(return_value=movie)
        result = await service.feedback(user_id, 202, "improvement", "Make the answer more concise")
        assert result["ok"] is True
        assert result["inference_status"] == "queued"

    with SessionLocal() as db:
        feedback = db.scalar(
            __import__("sqlalchemy")
            .select(Feedback)
            .where(Feedback.user_id == user_id, Feedback.movie_id == movie_id)
        )
        assert feedback is not None
        job = db.scalar(
            __import__("sqlalchemy")
            .select(FeedbackInferenceJob)
            .where(FeedbackInferenceJob.feedback_id == feedback.id)
        )
        assert job is not None
        assert job.status == "pending"


@pytest.mark.asyncio
async def test_feedback_request_is_idempotent_for_reused_key(isolated_database):
    with SessionLocal() as db:
        user = User(
            username="feedback-idempotent-user",
            email="feedback-idempotent@example.test",
            email_verified=True,
            status="verified",
            password_hash="hash",
        )
        movie = Movie(tmdb_id=303, title="Idempotent Movie")
        db.add_all([user, movie])
        db.commit()
        user_id = user.id

        service = MovieStateService(db, object())
        service.ensure_movie = AsyncMock(return_value=movie)
        first = await service.feedback(user_id, 303, "product", "Helpful", idempotency_key="retry-key-303")
        second = await service.feedback(user_id, 303, "product", "Helpful", idempotency_key="retry-key-303")

        assert first["ok"] is True
        assert first["inference_status"] == "queued"
        assert second["ok"] is True
        assert second["inference_status"] == "already_queued"
        assert second["feedback_id"] == first["feedback_id"]

    with SessionLocal() as db:
        assert db.query(Feedback).filter(Feedback.user_id == user_id).count() == 1
        assert db.query(FeedbackInferenceJob).filter(FeedbackInferenceJob.user_id == user_id).count() == 1


def test_worker_completes_feedback_job_and_is_idempotent(isolated_database, monkeypatch):
    _, job_id, user_id = seed_feedback()
    calls = []

    def fake_infer(self, uid, text, movie_title=None, source_message_id=None):
        calls.append((uid, text, movie_title))

    monkeypatch.setattr(
        "app.jobs.process_feedback_inference.MemoryService.infer_from_feedback",
        fake_infer,
    )
    with SessionLocal() as db:
        result = run_pending_feedback_jobs_once(db, worker_id="w1")
    assert result == {"claimed": 1, "completed": 1, "failed": 0}
    assert calls == [(user_id, "Great", "Example Movie")]

    with SessionLocal() as db:
        job = db.get(FeedbackInferenceJob, job_id)
        assert job.status == "complete"
        assert job.completed_at is not None
        assert job.locked_at is None

    with SessionLocal() as db:
        result = run_pending_feedback_jobs_once(db, worker_id="w2")
    assert result["claimed"] == 0


def test_worker_retries_failed_inference(isolated_database, monkeypatch):
    _, job_id, _ = seed_feedback()

    def fail(*args, **kwargs):
        raise RuntimeError("inference unavailable")

    monkeypatch.setattr("app.jobs.process_feedback_inference.MemoryService.infer_from_feedback", fail)
    with SessionLocal() as db:
        result = run_pending_feedback_jobs_once(db, worker_id="w1")
    assert result["claimed"] == 1
    assert result["failed"] == 0

    with SessionLocal() as db:
        job = db.get(FeedbackInferenceJob, job_id)
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.last_error == "RuntimeError"
        available_at = job.available_at
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=UTC)
        assert available_at > datetime.now(UTC) - timedelta(seconds=1)


def test_feedback_job_is_deleted_with_parent_feedback(isolated_database):
    feedback_id, job_id, _ = seed_feedback()
    with SessionLocal() as db:
        db.execute(Feedback.__table__.delete().where(Feedback.id == feedback_id))
        db.commit()
        assert db.get(FeedbackInferenceJob, job_id) is None


def test_feedback_job_indexes_exist(isolated_database):
    names = {index.name for index in FeedbackInferenceJob.__table__.indexes}
    assert {
        "ix_feedback_inference_jobs_user_id",
        "ix_feedback_inference_jobs_status",
        "ix_feedback_inference_jobs_available_at",
        "ix_feedback_inference_jobs_locked_at",
    } <= names
