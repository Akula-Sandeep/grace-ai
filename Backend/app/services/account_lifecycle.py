from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import MetaData, Table, delete, inspect, text, update

from app.config import settings
from app.database import AuditLog, SessionLocal, User


def purge_deleted_accounts(*, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.account_deletion_grace_days)
    purged = 0
    with SessionLocal.begin() as db:
        expired_ids = [
            int(user_id)
            for (user_id,) in db.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE status = 'deleted' AND deleted_at IS NOT NULL "
                    "AND deleted_at <= :cutoff"
                ),
                {"cutoff": cutoff},
            )
        ]
        inspector = inspect(db.bind)
        user_tables = [
            table
            for table in inspector.get_table_names()
            if "user_id" in {column["name"] for column in inspector.get_columns(table)}
        ]
        for user_id in expired_ids:
            user_hash = hashlib.sha256(f"grace-user:{user_id}".encode()).hexdigest()
            for table in user_tables:
                if table == "audit_log":
                    rows = db.execute(
                        text("SELECT id, metadata_json FROM audit_log WHERE user_id = :user_id"),
                        {"user_id": user_id},
                    ).all()
                    for audit_id, metadata in rows:
                        try:
                            details = json.loads(metadata) if metadata else {}
                        except (TypeError, ValueError):
                            details = {}
                        details["user_hash"] = user_hash
                        db.execute(
                            update(AuditLog)
                            .where(AuditLog.id == audit_id)
                            .values(
                                user_id=None,
                                metadata_json=json.dumps(details, sort_keys=True),
                            )
                        )
                elif table != "users":
                    # Reflected from the live schema: no SQL is built from strings.
                    reflected = Table(table, MetaData(), autoload_with=db.bind)
                    db.execute(delete(reflected).where(reflected.c.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            purged += 1
    return purged
