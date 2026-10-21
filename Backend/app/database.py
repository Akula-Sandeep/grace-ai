from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    desc,
    event,
    false,
    text,
    true,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app import config as config_module
from app.embeddings import VECTOR_DIMENSION
from app.observability import log_event

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    log_event(
        "database_vector_fallback",
        level=logging.WARNING,
        details={"reason": "optional_dependency_missing", "package": "pgvector"},
    )
    Vector = None


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_username_lower", text("lower(username)"), unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taste_profile_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(255), index=True, unique=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True, default=lambda: str(uuid.uuid4()))
    prev_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    idle_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    absolute_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmailOTP(Base):
    __tablename__ = "otp_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="verification", index=True)
    otp_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_used: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


OTPVerification = EmailOTP


class PendingEmailChange(Base):
    __tablename__ = "pending_email_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    otp_hash: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "ix_conversations_user_visibility_order",
            "user_id",
            "is_deleted",
            "is_archived",
            desc("is_pinned"),
            desc("updated_at"),
            desc("id"),
        ),
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New movie conversation")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    summary_through_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title_user_set: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The project this chat belongs to (its instructions apply to every turn); None: no project.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )


class Project(Base):
    """A named group of chats with standing instructions ("Date night": "We're two, keep it under 2 hours")."""

    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_user_deleted_updated", "user_id", "is_deleted", desc("updated_at")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SharedLink(Base):
    """A public, read-only link to one branch of a conversation, as it was when shared."""

    __tablename__ = "shared_links"
    __table_args__ = (Index("ix_shared_links_conversation", "conversation_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # The last message included; the shared page shows this message and its ancestors only.
    through_message_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("uuid_id", name="uq_messages_uuid_id"),
        UniqueConstraint("user_id", "client_message_id", name="uq_messages_user_client_id"),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_messages_parent_id", "parent_id"),
        Index(
            "ix_messages_convo_deleted_created",
            "conversation_id",
            "is_deleted",
            "created_at",
        ),
        Index(
            "ix_messages_convo_deleted_created_id",
            "conversation_id",
            "is_deleted",
            desc("created_at"),
            desc("id"),
        ),
    )
    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    client_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="complete", nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Attributable sources the answer was synthesised from (provenance UI).
    sources_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feedback: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Why an answer was marked not helpful (one of FEEDBACK_REASONS) and the user's own words.
    feedback_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Working state after this assistant reply; the next turn on this branch starts from it.
    turn_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Movie(Base):
    __tablename__ = "movies"
    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    original_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    release_year: Mapped[int | None] = mapped_column(nullable=True, index=True)
    runtime: Mapped[int | None] = mapped_column(nullable=True, index=True)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    backdrop_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MovieReference(Base):
    __tablename__ = "movie_references"
    __table_args__ = (
        UniqueConstraint("message_id", "tmdb_id", name="uq_message_movie_reference"),
        Index(
            "ix_movie_references_user_created_id",
            "user_id",
            desc("created_at"),
            desc("id"),
        ),
        Index(
            "ix_movie_references_conversation_created_id",
            "conversation_id",
            desc("created_at"),
            desc("id"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(300))
    ordinal: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(80), default="tool_result")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WatchlistItem(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "movie_id", name="uq_watchlist_user_movie"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchedItem(Base):
    __tablename__ = "watched"
    __table_args__ = (UniqueConstraint("user_id", "movie_id", name="uq_watched_user_movie"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    watched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("user_id", "movie_id", name="uq_rating_user_movie"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    rating: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_feedback_user_idempotency"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=True, index=True
    )
    feedback_type: Mapped[str] = mapped_column(String(80))
    text: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OTPDeliveryJob(Base):
    __tablename__ = "otp_delivery_jobs"
    __table_args__ = (Index("ix_otp_delivery_jobs_status_available", "status", "available_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    code_ciphertext: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(32), default="verification", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    __table_args__ = (Index("ix_export_jobs_user_status", "user_id", "status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    download_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GuestAdoption(Base):
    __tablename__ = "guest_adoptions"
    __table_args__ = (
        UniqueConstraint("user_id", "guest_session_id", name="uq_guest_adoptions_user_session"),
        Index("ix_guest_adoptions_user_id", "user_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    guest_session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    watchlist_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    watched_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FeedbackInferenceJob(Base):
    __tablename__ = "feedback_inference_jobs"
    __table_args__ = (UniqueConstraint("feedback_id", name="uq_feedback_inference_job_feedback"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("feedback.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Preference(Base):
    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "dimension", "value", "polarity", name="uq_preference"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dimension: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(String(300))
    polarity: Mapped[str] = mapped_column(String(20), default="prefer")
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(30), default="inferred")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserPersonalizationSetting(Base):
    __tablename__ = "user_personalization_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # How long answers are by default: brief | balanced | detailed (the request in hand wins).
    answer_style: Mapped[str] = mapped_column(
        String(16), default="balanced", server_default="balanced", nullable=False
    )
    daily_brief_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PushSubscription(Base):
    """A browser endpoint the user allowed Grace to send notifications to (Web Push)."""

    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def branch_lineage_ids(db, conversation_id, start_uuid: str | None, *, max_depth: int = 500) -> list[int]:
    """Ids of a message and its ancestors (nearest first), in one recursive query.

    Conversations branch (edits, regenerations), so "the history" is the chain
    of parents above the current message. Walking it one query per message made
    every turn slower as a chat grew; this is a single bounded query.
    """
    from sqlalchemy import literal, select
    from sqlalchemy.orm import aliased

    if not start_uuid:
        return []
    base = (
        select(Message.id, Message.parent_id, literal(1).label("depth"))
        .where(Message.uuid_id == start_uuid, Message.conversation_id == conversation_id)
        .cte("lineage", recursive=True)
    )
    parent = aliased(Message)
    step = select(parent.id, parent.parent_id, (base.c.depth + 1).label("depth")).where(
        parent.uuid_id == base.c.parent_id,
        parent.conversation_id == conversation_id,
        base.c.depth < max_depth,
    )
    lineage = base.union_all(step)
    rows = db.execute(select(lineage.c.id, lineage.c.depth).order_by(lineage.c.depth)).all()
    seen: set[int] = set()
    ordered = []
    for message_id, _depth in rows:
        if message_id not in seen:
            seen.add(message_id)
            ordered.append(message_id)
    return ordered


class ChatAction(Base):
    """A side effect performed for one chat request (exactly-once ledger)."""

    __tablename__ = "chat_actions"
    __table_args__ = (
        UniqueConstraint("user_id", "request_key", "action_key", name="uq_chat_actions_request"),
        Index("ix_chat_actions_user_created", "user_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    action_key: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Notification(Base):
    """A user-visible notification; reminders are notifications with a due time.

    A notification is shown only once ``due_at`` has passed, so scheduled
    reminders need no separate dispatcher process.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_due", "user_id", "due_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="reminder", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    movie_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # In-app deep link for the notification ("/c/42", "/movie/157336"); never an external URL.
    link_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # When it was sent to the user's devices (Web Push), so it is pushed once.
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserInterfaceSetting(Base):
    __tablename__ = "user_interface_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    theme: Mapped[str] = mapped_column(String(16), default="system", nullable=False)
    density: Mapped[str] = mapped_column(String(16), default="comfortable", nullable=False)
    region: Mapped[str] = mapped_column(String(2), default="NL", nullable=False)
    motion: Mapped[str] = mapped_column(String(16), default="system", server_default="system", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "content_hash",
            name="uq_memories_user_content_hash",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(30), default="episodic")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list | None] = mapped_column(
        (Vector(VECTOR_DIMENSION).with_variant(JSON(), "sqlite") if Vector is not None else JSON),
        nullable=True,
    )


@event.listens_for(Memory, "before_insert")
def _populate_memory_content_hash(_mapper, _connection, target):
    if not target.content_hash and target.content:
        target.content_hash = sha256(" ".join(target.content.casefold().split()).encode("utf-8")).hexdigest()


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int | None] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(80), default="web")
    document_type: Mapped[str] = mapped_column(String(100), default="article")
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding: Mapped[list | None] = mapped_column(
        (Vector(VECTOR_DIMENSION).with_variant(JSON(), "sqlite") if Vector is not None else JSON),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + database_url[len("sqlite+aiosqlite://") :]
    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+asyncpg://") :]
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    if database_url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + database_url[len("postgresql+psycopg2://") :]
    return database_url


# Keep connection settings conservative; PostgreSQL deployment can use its own pool defaults.
def _build_engine(database_url: str | None = None):
    url = _normalize_database_url(database_url or config_module.settings.database_url)
    kw = {"future": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
    else:
        kw.update(
            {
                "pool_pre_ping": True,
                "pool_recycle": 1800,
                "pool_size": config_module.settings.db_pool_size,
                "max_overflow": config_module.settings.db_max_overflow,
            }
        )
    return create_engine(url, **kw)


def _configure_sqlite_connection(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class _EngineProxy:
    def __init__(self):
        self._engine = None

    def _refresh(self):
        global SessionLocal
        desired_url = _normalize_database_url(config_module.settings.database_url)
        if self._engine is None or self._engine.url.render_as_string(hide_password=False) != desired_url:
            self._engine = _build_engine(desired_url)
            if "SessionLocal" in globals() and SessionLocal is not None:
                SessionLocal.configure(bind=self._engine)
            if self._engine.dialect.name == "sqlite":
                event.listen(self._engine, "connect", _configure_sqlite_connection)
        return self._engine

    @property
    def url(self):
        return self._refresh().url

    def __getattr__(self, name):
        return getattr(self._refresh(), name)


engine = _EngineProxy()
SessionLocal = sessionmaker(bind=engine._refresh(), expire_on_commit=False)


def _call_with_session(factory, fn: Callable):
    with factory() as db:
        return fn(db)


async def run_db[DBResult](
    fn: Callable[[object], DBResult],
    *,
    session_factory=None,
) -> DBResult:
    """Run one synchronous SQLAlchemy unit on a worker thread.

    The callable owns a fresh session for its entire synchronous transaction.
    ORM sessions must never cross an await boundary or be shared between
    the event-loop thread and the worker thread.
    """
    return await asyncio.to_thread(
        _call_with_session,
        session_factory or SessionLocal,
        fn,
    )


def init_db():
    raise RuntimeError("Automatic schema creation is disabled; run `alembic upgrade head`.")
