from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select

from app.config import settings
from app.database import ExportJob, OTPDeliveryJob, SessionLocal
from app.jobs.process_feedback_inference import run_pending_feedback_jobs_once
from app.observability import logger
from app.services.account_lifecycle import purge_deleted_accounts as _purge
from app.services.email_service import send_otp_email
from app.services.export_service import generate_export_job
from app.services.otp_delivery_service import decrypt_code

_BATCH_SIZE = 25
_LEASE_SECONDS = 300
_MAX_ATTEMPTS = 5


def _claim_otp_jobs(db, worker_id: str, now: datetime):
    stale = now - timedelta(seconds=_LEASE_SECONDS)
    query = (
        select(OTPDeliveryJob)
        .where(
            or_(
                and_(
                    OTPDeliveryJob.status == "pending",
                    OTPDeliveryJob.available_at <= now,
                ),
                and_(
                    OTPDeliveryJob.status == "running",
                    OTPDeliveryJob.locked_at.is_not(None),
                    OTPDeliveryJob.locked_at <= stale,
                ),
            ),
            OTPDeliveryJob.attempts < _MAX_ATTEMPTS,
        )
        .order_by(OTPDeliveryJob.id.asc())
        .limit(_BATCH_SIZE)
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    jobs = db.scalars(query).all()
    for job in jobs:
        job.status = "running"
        job.locked_at = now
        job.locked_by = worker_id
        job.attempts += 1
    if jobs:
        db.commit()
    return jobs


def process_otp_jobs_once(worker_id: str) -> int:
    with SessionLocal() as db:
        jobs = _claim_otp_jobs(db, worker_id, datetime.now(UTC))
    processed = 0
    for job in jobs:
        try:
            code = decrypt_code(job.code_ciphertext)
            result = send_otp_email(job.email, code, job.purpose or "verification")
            if hasattr(result, "__await__"):
                import asyncio

                asyncio.run(result)
            with SessionLocal() as db:
                current = db.get(OTPDeliveryJob, job.id)
                if current:
                    current.status = "sent"
                    current.sent_at = datetime.now(UTC)
                    current.locked_at = None
                    current.locked_by = None
                    current.last_error = None
                    db.commit()
            processed += 1
        except Exception as exc:  # worker loop isolation boundary (reviewed broad catch)
            with SessionLocal() as db:
                current = db.get(OTPDeliveryJob, job.id)
                if current:
                    current.locked_at = None
                    current.locked_by = None
                    current.last_error = type(exc).__name__[:500]
                    if current.attempts >= _MAX_ATTEMPTS:
                        current.status = "failed"
                    else:
                        current.status = "pending"
                        current.available_at = datetime.now(UTC) + timedelta(
                            seconds=min(300, 2**current.attempts * 5)
                        )
                    db.commit()
            logger.exception("otp_delivery_job_failed job_id=%s", job.id)
    return processed


def process_export_jobs_once(worker_id: str) -> int:
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=_LEASE_SECONDS)
    with SessionLocal() as db:
        terminal_stale = db.scalars(
            select(ExportJob).where(
                ExportJob.status == "running",
                ExportJob.locked_at.is_not(None),
                ExportJob.locked_at <= stale,
                ExportJob.attempts >= _MAX_ATTEMPTS,
            )
        ).all()
        for job in terminal_stale:
            job.status = "failed"
            job.stage = "failed"
            job.progress = 100
            job.last_error = "max_attempts_exceeded"
            job.locked_at = None
            job.locked_by = None
        if terminal_stale:
            db.commit()

        query = (
            select(ExportJob.id)
            .where(
                or_(
                    and_(ExportJob.status == "pending"),
                    and_(
                        ExportJob.status == "running",
                        ExportJob.locked_at.is_not(None),
                        ExportJob.locked_at <= stale,
                    ),
                ),
                ExportJob.attempts < _MAX_ATTEMPTS,
            )
            .order_by(ExportJob.id.asc())
            .limit(2)
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job_ids = [int(x) for x in db.scalars(query).all()]
        for job_id in job_ids:
            job = db.get(ExportJob, job_id)
            job.status = "running"
            job.locked_at = now
            job.locked_by = worker_id
            job.attempts += 1
            db.commit()
    for job_id in job_ids:
        try:
            generate_export_job(job_id, worker_id=worker_id)
        except Exception:  # worker loop isolation boundary (reviewed broad catch)
            # The job row records its own failure state; keep draining the queue.
            logger.exception("export_job_failed job_id=%s", job_id)
    return len(job_ids)


def cleanup_exports_once() -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=settings.export_retention_hours)
    removed = 0
    with SessionLocal() as db:
        jobs = db.scalars(select(ExportJob).where(ExportJob.created_at < cutoff)).all()
        for job in jobs:
            if job.artifact_path:
                Path(job.artifact_path).unlink(missing_ok=True)
            db.delete(job)
            removed += 1
        db.commit()
    return removed


def _watch_providers(tmdb_id: int, region: str) -> list[str]:
    import asyncio

    from app.services.tmdb_client import TMDBClient

    async def lookup():
        client = TMDBClient()
        try:
            providers = await client.get_watch_providers(tmdb_id, region)
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await close()
        return [p["provider_name"] for p in providers.get("flatrate") or [] if p.get("provider_name")]

    return asyncio.run(lookup())


def _collection_for(tmdb_id: int) -> dict | None:
    import asyncio

    from app.services.tmdb_client import TMDBClient

    async def lookup():
        client = TMDBClient()
        try:
            details = await client.get_movie_details(tmdb_id)
            collection = (details or {}).get("belongs_to_collection") or {}
            if not collection.get("id"):
                return None
            return await client._get(f"/collection/{collection['id']}", {"language": "en-US"})
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await close()

    return asyncio.run(lookup())


def proactive_notifications_once() -> int:
    """Release + availability notifications for saved films (bounded, idempotent)."""
    import httpx
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.proactive import (
        availability_notifications_once,
        recommendation_follow_ups_once,
        release_notifications_once,
        sequel_notifications_once,
    )

    created = 0
    try:
        with SessionLocal() as db:
            created += release_notifications_once(db)
        with SessionLocal() as db:
            created += recommendation_follow_ups_once(db)
        if settings.tmdb_api_key:
            with SessionLocal() as db:
                created += availability_notifications_once(db, _watch_providers)
            with SessionLocal() as db:
                created += sequel_notifications_once(db, _collection_for)
    except (SQLAlchemyError, httpx.HTTPError, RuntimeError, ValueError, OSError):
        logger.exception("proactive_notifications_failed")
    return created


def daily_briefs_once() -> int:
    """Today's brief for the users who turned it on (idempotent per day)."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.daily_brief import daily_briefs_once as create_briefs

    try:
        with SessionLocal() as db:
            return create_briefs(db)
    except SQLAlchemyError:
        logger.exception("daily_brief_failed")
        return 0


def push_notifications_once() -> int:
    """Send due notifications to the devices their users allowed (Web Push; off without keys)."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.web_push import enabled, push_due_notifications_once

    if not enabled():
        return 0
    try:
        with SessionLocal() as db:
            return push_due_notifications_once(db)
    except SQLAlchemyError:
        logger.exception("web_push_failed")
        return 0


def main() -> None:
    worker_id = f"bg-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    last_purge = 0.0
    last_export_cleanup = 0.0
    last_proactive = 0.0
    last_brief = 0.0
    last_push = 0.0
    while True:
        with SessionLocal() as db:
            run_pending_feedback_jobs_once(db, worker_id=worker_id)
        process_otp_jobs_once(worker_id)
        process_export_jobs_once(worker_id)
        now = time.monotonic()
        if now - last_purge >= 60:
            try:
                _purge()
            except Exception:  # worker loop isolation boundary (reviewed broad catch)
                logger.exception("deleted_account_purge_failed")
            last_purge = now
        if now - last_export_cleanup >= 300:
            try:
                cleanup_exports_once()
            except Exception:  # worker loop isolation boundary (reviewed broad catch)
                logger.exception("export_cleanup_failed")
            last_export_cleanup = now
        if now - last_proactive >= 6 * 60 * 60:
            proactive_notifications_once()
            last_proactive = now
        if now - last_brief >= 60 * 60:
            daily_briefs_once()
            last_brief = now
        if now - last_push >= 60:
            push_notifications_once()
            last_push = now
        time.sleep(1.0)


if __name__ == "__main__":
    main()
