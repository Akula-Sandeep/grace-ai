"""Add feedback idempotency and durable OTP/export jobs."""

import sqlalchemy as sa

from alembic import op

revision = "0022_lifecycle_jobs_idempotency"
down_revision = "0021_feedback_inference_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feedback", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_unique_constraint("uq_feedback_user_idempotency", "feedback", ["user_id", "idempotency_key"])

    op.create_table(
        "otp_delivery_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("code_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "purpose",
            sa.String(length=32),
            nullable=False,
            server_default="verification",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_otp_delivery_jobs_email", "otp_delivery_jobs", ["email"])
    op.create_index("ix_otp_delivery_jobs_purpose", "otp_delivery_jobs", ["purpose"])
    op.create_index("ix_otp_delivery_jobs_status", "otp_delivery_jobs", ["status"])
    op.create_index("ix_otp_delivery_jobs_available_at", "otp_delivery_jobs", ["available_at"])
    op.create_index("ix_otp_delivery_jobs_locked_at", "otp_delivery_jobs", ["locked_at"])
    op.create_index(
        "ix_otp_delivery_jobs_status_available",
        "otp_delivery_jobs",
        ["status", "available_at"],
    )

    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("artifact_size", sa.Integer(), nullable=True),
        sa.Column("download_token_hash", sa.String(length=64), nullable=True),
        sa.Column("download_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("download_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("download_token_hash", name="uq_export_jobs_download_token_hash"),
    )
    op.create_index("ix_export_jobs_user_id", "export_jobs", ["user_id"])
    op.create_index("ix_export_jobs_status", "export_jobs", ["status"])
    op.create_index("ix_export_jobs_created_at", "export_jobs", ["created_at"])
    op.create_index("ix_export_jobs_user_status", "export_jobs", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_export_jobs_user_status", table_name="export_jobs")
    op.drop_index("ix_export_jobs_created_at", table_name="export_jobs")
    op.drop_index("ix_export_jobs_status", table_name="export_jobs")
    op.drop_index("ix_export_jobs_user_id", table_name="export_jobs")
    op.drop_table("export_jobs")
    op.drop_index("ix_otp_delivery_jobs_status_available", table_name="otp_delivery_jobs")
    op.drop_index("ix_otp_delivery_jobs_locked_at", table_name="otp_delivery_jobs")
    op.drop_index("ix_otp_delivery_jobs_available_at", table_name="otp_delivery_jobs")
    op.drop_index("ix_otp_delivery_jobs_status", table_name="otp_delivery_jobs")
    op.drop_index("ix_otp_delivery_jobs_purpose", table_name="otp_delivery_jobs")
    op.drop_index("ix_otp_delivery_jobs_email", table_name="otp_delivery_jobs")
    op.drop_table("otp_delivery_jobs")
    op.drop_constraint("uq_feedback_user_idempotency", "feedback", type_="unique")
    op.drop_column("feedback", "idempotency_key")
