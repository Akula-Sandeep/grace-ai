"""Harden refresh-session lifecycle metadata.

Revision ID: 0014_auth_session_hardening
Revises: 0013_message_inference_metadata
"""

import sqlalchemy as sa

from alembic import op

revision = "0014_auth_session_hardening"
down_revision = "0013_message_inference_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("refresh_sessions")}
    additions = (
        ("family_id", sa.String(64)),
        ("prev_token_hash", sa.String(255)),
        ("idle_expires_at", sa.DateTime(timezone=True)),
        ("absolute_expires_at", sa.DateTime(timezone=True)),
        ("revoked_reason", sa.String(64)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column("refresh_sessions", sa.Column(name, column_type, nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("refresh_sessions")}
    for name in (
        "family_id",
        "prev_token_hash",
        "idle_expires_at",
        "absolute_expires_at",
    ):
        index_name = f"ix_refresh_sessions_{name}"
        if index_name not in indexes:
            op.create_index(index_name, "refresh_sessions", [name], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("refresh_sessions")}
    for name in (
        "family_id",
        "prev_token_hash",
        "idle_expires_at",
        "absolute_expires_at",
    ):
        index = f"ix_refresh_sessions_{name}"
        if index in indexes:
            op.drop_index(index, table_name="refresh_sessions")
    columns = {column["name"] for column in inspector.get_columns("refresh_sessions")}
    for name in (
        "revoked_reason",
        "absolute_expires_at",
        "idle_expires_at",
        "prev_token_hash",
        "family_id",
    ):
        if name in columns:
            op.drop_column("refresh_sessions", name)
