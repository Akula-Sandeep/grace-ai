"""Add security audit records and account deletion grace metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0016_security_audit_and_deletion"
down_revision = "0015_otp_purposes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "deleted_at" not in user_columns:
        op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_users_deleted_at", "users", ["deleted_at"], unique=False)
    if not inspector.has_table("audit_log"):
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column(
                "session_id",
                sa.Integer(),
                sa.ForeignKey("refresh_sessions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=True),
        )
        op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
        op.create_index("ix_audit_log_event_type", "audit_log", ["event_type"])
        op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("audit_log"):
        op.drop_table("audit_log")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "deleted_at" in columns:
        op.drop_index("ix_users_deleted_at", table_name="users")
        op.drop_column("users", "deleted_at")
