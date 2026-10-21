"""Create the frozen initial database schema.

This revision intentionally does not import the live ORM models. Later model
changes must be represented by a new revision instead of changing history.
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _vector_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        from pgvector.sqlalchemy import Vector

        return Vector(384)
    return sa.JSON()


def upgrade():
    _vector = _vector_type()
    op.create_table(
        "movies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("imdb_id", sa.String(length=30), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("original_title", sa.String(length=300), nullable=True),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("runtime", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("poster_path", sa.String(length=500), nullable=True),
        sa.Column("backdrop_path", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_movies_imdb_id"), "movies", ["imdb_id"], unique=False)
    op.create_index(op.f("ix_movies_release_year"), "movies", ["release_year"], unique=False)
    op.create_index(op.f("ix_movies_runtime"), "movies", ["runtime"], unique=False)
    op.create_index(op.f("ix_movies_tmdb_id"), "movies", ["tmdb_id"], unique=True)
    op.create_table(
        "otp_verifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("otp_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("is_used", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_otp_verifications_email"), "otp_verifications", ["email"], unique=False)
    op.create_index(
        op.f("ix_otp_verifications_purpose"),
        "otp_verifications",
        ["purpose"],
        unique=False,
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_email_verified"), "users", ["email_verified"], unique=False)
    op.create_index(op.f("ix_users_is_active"), "users", ["is_active"], unique=False)
    op.create_index(op.f("ix_users_status"), "users", ["status"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_through_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("title_user_set", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversations_is_deleted"),
        "conversations",
        ["is_deleted"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversations_updated_at"),
        "conversations",
        ["updated_at"],
        unique=False,
    )
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("feedback_type", sa.String(length=80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_movie_id"), "feedback", ["movie_id"], unique=False)
    op.create_index(op.f("ix_feedback_user_id"), "feedback", ["user_id"], unique=False)
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("embedding", _vector, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_documents_content_hash"),
        "knowledge_documents",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_documents_movie_id"),
        "knowledge_documents",
        ["movie_id"],
        unique=False,
    )
    op.create_table(
        "pending_email_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("otp_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pending_email_changes_email"),
        "pending_email_changes",
        ["email"],
        unique=True,
    )
    op.create_index(
        op.f("ix_pending_email_changes_user_id"),
        "pending_email_changes",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("dimension", sa.String(length=120), nullable=False),
        sa.Column("value", sa.String(length=300), nullable=False),
        sa.Column("polarity", sa.String(length=20), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dimension", "value", "polarity", name="uq_preference"),
    )
    op.create_index(op.f("ix_preferences_dimension"), "preferences", ["dimension"], unique=False)
    op.create_index(op.f("ix_preferences_user_id"), "preferences", ["user_id"], unique=False)
    op.create_table(
        "ratings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("movie_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "movie_id", name="uq_rating_user_movie"),
    )
    op.create_index(op.f("ix_ratings_movie_id"), "ratings", ["movie_id"], unique=False)
    op.create_index(op.f("ix_ratings_user_id"), "ratings", ["user_id"], unique=False)
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_hash", sa.String(length=255), nullable=False),
        sa.Column("family_id", sa.String(length=64), nullable=False),
        sa.Column("prev_token_hash", sa.String(length=255), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_refresh_sessions_absolute_expires_at"),
        "refresh_sessions",
        ["absolute_expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refresh_sessions_family_id"),
        "refresh_sessions",
        ["family_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refresh_sessions_idle_expires_at"),
        "refresh_sessions",
        ["idle_expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refresh_sessions_prev_token_hash"),
        "refresh_sessions",
        ["prev_token_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refresh_sessions_refresh_hash"),
        "refresh_sessions",
        ["refresh_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_refresh_sessions_user_id"),
        "refresh_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "user_interface_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("theme", sa.String(length=16), nullable=False),
        sa.Column("density", sa.String(length=16), nullable=False),
        sa.Column("region", sa.String(length=2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_interface_settings_user_id"),
        "user_interface_settings",
        ["user_id"],
        unique=True,
    )
    op.create_table(
        "user_personalization_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_personalization_settings_user_id"),
        "user_personalization_settings",
        ["user_id"],
        unique=True,
    )
    op.create_table(
        "watched",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=False),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "movie_id", name="uq_watched_user_movie"),
    )
    op.create_index(op.f("ix_watched_movie_id"), "watched", ["movie_id"], unique=False)
    op.create_index(op.f("ix_watched_user_id"), "watched", ["user_id"], unique=False)
    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "movie_id", name="uq_watchlist_user_movie"),
    )
    op.create_index(op.f("ix_watchlist_movie_id"), "watchlist", ["movie_id"], unique=False)
    op.create_index(op.f("ix_watchlist_user_id"), "watchlist", ["user_id"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["refresh_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"], unique=False)
    op.create_index(op.f("ix_audit_log_event_type"), "audit_log", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_log_user_id"), "audit_log", ["user_id"], unique=False)
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("client_message_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.String(length=8), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_message_id", name="uq_messages_user_client_id"),
        sa.UniqueConstraint("uuid_id", name="uq_messages_uuid_id"),
    )
    op.create_index(
        op.f("ix_messages_conversation_id"),
        "messages",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_messages_conversation_id_created_at",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_messages_convo_deleted_created",
        "messages",
        ["conversation_id", "is_deleted", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_messages_created_at"), "messages", ["created_at"], unique=False)
    op.create_index(op.f("ix_messages_is_deleted"), "messages", ["is_deleted"], unique=False)
    op.create_index("ix_messages_parent_id", "messages", ["parent_id"], unique=False)
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("memory_type", sa.String(length=30), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", _vector, nullable=True),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_memories_user_id"), "memories", ["user_id"], unique=False)
    op.create_table(
        "movie_references",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "tmdb_id", name="uq_message_movie_reference"),
    )
    op.create_index(
        op.f("ix_movie_references_conversation_id"),
        "movie_references",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_movie_references_created_at"),
        "movie_references",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_movie_references_message_id"),
        "movie_references",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_movie_references_tmdb_id"),
        "movie_references",
        ["tmdb_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_movie_references_user_id"),
        "movie_references",
        ["user_id"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade():
    op.drop_table("movie_references")
    op.drop_table("memories")
    op.drop_table("messages")
    op.drop_table("audit_log")
    op.drop_table("watchlist")
    op.drop_table("watched")
    op.drop_table("user_personalization_settings")
    op.drop_table("user_interface_settings")
    op.drop_table("refresh_sessions")
    op.drop_table("ratings")
    op.drop_table("preferences")
    op.drop_table("pending_email_changes")
    op.drop_table("knowledge_documents")
    op.drop_table("feedback")
    op.drop_table("conversations")
    op.drop_table("users")
    op.drop_table("otp_verifications")
    op.drop_table("movies")
