from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import (
    Conversation,
    ExportJob,
    Feedback,
    Memory,
    Message,
    Movie,
    Preference,
    Rating,
    SessionLocal,
    User,
    WatchedItem,
    WatchlistItem,
)
from app.services.otp_delivery_service import encrypt_download_token

MAX_ROWS = 200_000


def hash_download_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8")


def _write_query_jsonl(zf: zipfile.ZipFile, name: str, rows, encode_row) -> int:
    count = 0
    with zf.open(name, "w") as out:
        for row in rows:
            count += 1
            if count > MAX_ROWS:
                raise ValueError("export_row_limit_exceeded")
            out.write(_json_line(encode_row(row)))
    return count


def _iter_batches(model, *, user_id: int, encode_row, batch_size: int = 500):
    """Yield bounded primary-key batches without holding a DB session between batches."""
    last_id = 0
    emitted = 0
    while True:
        with SessionLocal() as db:
            rows = (
                db.execute(
                    select(model)
                    .where(model.user_id == user_id, model.id > last_id)
                    .order_by(model.id.asc())
                    .limit(batch_size)
                )
                .scalars()
                .all()
            )
        if not rows:
            return
        for row in rows:
            emitted += 1
            if emitted > MAX_ROWS:
                raise ValueError("export_row_limit_exceeded")
            yield encode_row(row)
        last_id = rows[-1].id


def _iter_joined_movie_batches(model, *, user_id: int, encode_row, batch_size: int = 500):
    """Yield bounded collection batches with movie data joined in SQL."""
    last_id = 0
    emitted = 0
    while True:
        with SessionLocal() as db:
            rows = db.execute(
                select(model, Movie.tmdb_id, Movie.title)
                .join(Movie, Movie.id == model.movie_id)
                .where(model.user_id == user_id, model.id > last_id)
                .order_by(model.id.asc())
                .limit(batch_size)
            ).all()
        if not rows:
            return
        for item, tmdb_id, title in rows:
            emitted += 1
            if emitted > MAX_ROWS:
                raise ValueError("export_row_limit_exceeded")
            yield encode_row(item, tmdb_id, title)
        last_id = rows[-1][0].id


def generate_export_job(job_id: int, *, worker_id: str) -> None:
    Path(settings.export_dir).mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    artifact_name = f"grace-export-{job_id}-{secrets.token_hex(8)}.zip"
    final_path = Path(settings.export_dir) / artifact_name
    token = secrets.token_urlsafe(32)

    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        if job is None:
            return
        user_id = job.user_id
        job.status = "running"
        job.stage = "snapshot"
        job.progress = 5
        job.locked_at = now
        job.locked_by = worker_id
        db.commit()

    try:
        with SessionLocal() as db:
            user = db.execute(select(User.id, User.email, User.username).where(User.id == user_id)).first()
        if user is None:
            raise ValueError("export_user_not_found")

        with tempfile.NamedTemporaryFile(
            prefix="grace-export-",
            suffix=".zip",
            dir=settings.export_dir,
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)

        try:
            with zipfile.ZipFile(temp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "account.json",
                    json.dumps(
                        {
                            "exported_at": now.isoformat(),
                            "account": {
                                "user_id": user.id,
                                "email": user.email,
                                "username": user.username,
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                        indent=2,
                    ).encode("utf-8"),
                )

                with zf.open("conversations.ndjson", "w") as out:
                    for row in _iter_batches(
                        Conversation,
                        user_id=user_id,
                        encode_row=lambda c: {
                            "id": c.id,
                            "title": c.title,
                            "summary": c.summary,
                            "created_at": c.created_at,
                            "updated_at": c.updated_at,
                            "is_deleted": c.is_deleted,
                            "is_archived": c.is_archived,
                            "is_pinned": c.is_pinned,
                        },
                    ):
                        out.write(_json_line(row))

                _update_progress(job_id, 20, "messages")
                with zf.open("messages.ndjson", "w") as out:
                    for row in _iter_batches(
                        Message,
                        user_id=user_id,
                        encode_row=lambda m: {
                            "id": m.uuid_id,
                            "conversation_id": m.conversation_id,
                            "role": m.role,
                            "content": m.content,
                            "parent_id": m.parent_id,
                            "status": m.status,
                            "model": m.model,
                            "created_at": m.created_at,
                        },
                    ):
                        out.write(_json_line(row))

                _update_progress(job_id, 55, "collections")
                with zf.open("watchlist.ndjson", "w") as out:
                    for row in _iter_joined_movie_batches(
                        WatchlistItem,
                        user_id=user_id,
                        encode_row=lambda item, tmdb_id, title: {
                            "movie": {"tmdb_id": tmdb_id, "title": title},
                            "created_at": item.created_at,
                        },
                    ):
                        out.write(_json_line(row))
                with zf.open("watched.ndjson", "w") as out:
                    for row in _iter_joined_movie_batches(
                        WatchedItem,
                        user_id=user_id,
                        encode_row=lambda item, tmdb_id, title: {
                            "movie": {"tmdb_id": tmdb_id, "title": title},
                            "watched_at": item.watched_at,
                        },
                    ):
                        out.write(_json_line(row))
                with zf.open("ratings.ndjson", "w") as out:
                    for row in _iter_joined_movie_batches(
                        Rating,
                        user_id=user_id,
                        encode_row=lambda item, tmdb_id, title: {
                            "movie": {"tmdb_id": tmdb_id, "title": title},
                            "rating": item.rating,
                            "updated_at": item.updated_at,
                        },
                    ):
                        out.write(_json_line(row))

                _update_progress(job_id, 75, "personalization")
                with zf.open("preferences.ndjson", "w") as out:
                    for row in _iter_batches(
                        Preference,
                        user_id=user_id,
                        encode_row=lambda x: {
                            "id": x.id,
                            "dimension": x.dimension,
                            "value": x.value,
                            "polarity": x.polarity,
                            "strength": x.strength,
                            "confidence": x.confidence,
                            "evidence_count": x.evidence_count,
                        },
                    ):
                        out.write(_json_line(row))
                with zf.open("memories.ndjson", "w") as out:
                    for row in _iter_batches(
                        Memory,
                        user_id=user_id,
                        encode_row=lambda x: {
                            "id": x.id,
                            "content": x.content,
                            "type": x.memory_type,
                            "importance": x.importance,
                            "created_at": x.created_at,
                        },
                    ):
                        out.write(_json_line(row))
                with zf.open("feedback.ndjson", "w") as out:
                    for row in _iter_batches(
                        Feedback,
                        user_id=user_id,
                        encode_row=lambda x: {
                            "type": x.feedback_type,
                            "text": x.text,
                            "created_at": x.created_at,
                        },
                    ):
                        out.write(_json_line(row))

            size = temp_path.stat().st_size
            if size > settings.export_max_bytes:
                raise ValueError("export_size_limit_exceeded")
            os.replace(temp_path, final_path)
        finally:
            temp_path.unlink(missing_ok=True)

        expires = datetime.now(UTC) + timedelta(hours=settings.export_retention_hours)
        with SessionLocal() as db:
            job = db.get(ExportJob, job_id)
            if job is None:
                final_path.unlink(missing_ok=True)
                return
            job.status = "ready"
            job.stage = "ready"
            job.progress = 100
            job.artifact_path = str(final_path)
            job.artifact_size = final_path.stat().st_size
            job.download_token_hash = hash_download_token(token)
            job.download_token_ciphertext = encrypt_download_token(token)
            job.download_expires_at = expires
            job.completed_at = datetime.now(UTC)
            job.locked_at = None
            job.locked_by = None
            job.last_error = None
            db.commit()
    except Exception as exc:
        final_path.unlink(missing_ok=True)
        with SessionLocal() as db:
            job = db.get(ExportJob, job_id)
            if job:
                job.status = "failed"
                job.stage = "failed"
                job.progress = 100
                job.last_error = type(exc).__name__[:500]
                job.locked_at = None
                job.locked_by = None
                db.commit()
        raise


def _update_progress(job_id: int, progress: int, stage: str) -> None:
    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        if job:
            job.progress = progress
            job.stage = stage
            db.commit()
