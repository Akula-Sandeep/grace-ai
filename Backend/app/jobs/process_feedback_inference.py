from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.database import Feedback, FeedbackInferenceJob, Movie, SessionLocal
from app.memory import MemoryService
from app.observability import logger

_BATCH_SIZE = 25
_LEASE_SECONDS = 300
_MAX_ATTEMPTS = 5


def run_pending_feedback_jobs_once(
    db: Session,
    *,
    worker_id: str,
    batch_size: int = _BATCH_SIZE,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    stale_before = now - timedelta(seconds=_LEASE_SECONDS)

    query = (
        select(FeedbackInferenceJob)
        .where(
            or_(
                and_(
                    FeedbackInferenceJob.status == "pending",
                    FeedbackInferenceJob.available_at <= now,
                ),
                and_(
                    FeedbackInferenceJob.status == "running",
                    FeedbackInferenceJob.locked_at.is_not(None),
                    FeedbackInferenceJob.locked_at <= stale_before,
                ),
            ),
            FeedbackInferenceJob.attempts < _MAX_ATTEMPTS,
        )
        .order_by(FeedbackInferenceJob.id.asc())
        .limit(batch_size)
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)

    jobs = db.scalars(query).all()
    claimed = 0
    completed = 0
    failed = 0

    for job in jobs:
        job.status = "running"
        job.locked_at = now
        job.locked_by = worker_id
        job.attempts += 1
        claimed += 1
    if jobs:
        db.commit()

    for job in jobs:
        try:
            feedback = db.get(Feedback, job.feedback_id)
            if feedback is None:
                job.status = "complete"
                job.completed_at = datetime.now(UTC)
                job.last_error = None
                completed += 1
                db.commit()
                continue

            movie_title = None
            if feedback.movie_id:
                movie = db.get(Movie, feedback.movie_id)
                movie_title = movie.title if movie else None
            MemoryService(db).infer_from_feedback(
                feedback.user_id,
                feedback.text,
                movie_title=movie_title,
                source_message_id=None,
            )
            # `infer_from_feedback` commits its own durable memory writes.
            job.status = "complete"
            job.completed_at = datetime.now(UTC)
            job.locked_at = None
            job.locked_by = None
            job.last_error = None
            completed += 1
            db.commit()
        except (
            Exception
        ) as exc:  # pragma: no cover - exercised by worker/runtime drills  # worker loop isolation boundary (reviewed broad catch)
            db.rollback()
            refreshed = db.get(FeedbackInferenceJob, job.id)
            if refreshed is None:
                continue
            refreshed.locked_at = None
            refreshed.locked_by = None
            refreshed.last_error = type(exc).__name__[:500]
            if refreshed.attempts >= _MAX_ATTEMPTS:
                refreshed.status = "failed"
                failed += 1
            else:
                refreshed.status = "pending"
                refreshed.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2**refreshed.attempts * 5)
                )
            db.commit()
            logger.exception(
                "feedback_inference_failed job_id=%s feedback_id=%s attempts=%s",
                refreshed.id,
                refreshed.feedback_id,
                refreshed.attempts,
            )

    return {"claimed": claimed, "completed": completed, "failed": failed}


def main() -> None:
    worker_id = f"feedback-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    while True:
        with SessionLocal() as db:
            result = run_pending_feedback_jobs_once(db, worker_id=worker_id)
        if result["claimed"] == 0:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
