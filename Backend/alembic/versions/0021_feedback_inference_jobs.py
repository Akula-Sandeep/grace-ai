"""Add durable feedback inference jobs."""

import sqlalchemy as sa

from alembic import op

revision = "0021_feedback_inference_jobs"
down_revision = "0020_memory_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback_inference_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.Integer(),
            sa.ForeignKey("feedback.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("feedback_id", name="uq_feedback_inference_job_feedback"),
    )
    op.create_index("ix_feedback_inference_jobs_user_id", "feedback_inference_jobs", ["user_id"])
    op.create_index("ix_feedback_inference_jobs_status", "feedback_inference_jobs", ["status"])
    op.create_index(
        "ix_feedback_inference_jobs_available_at",
        "feedback_inference_jobs",
        ["available_at"],
    )
    op.create_index("ix_feedback_inference_jobs_locked_at", "feedback_inference_jobs", ["locked_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_inference_jobs_locked_at", table_name="feedback_inference_jobs")
    op.drop_index("ix_feedback_inference_jobs_available_at", table_name="feedback_inference_jobs")
    op.drop_index("ix_feedback_inference_jobs_status", table_name="feedback_inference_jobs")
    op.drop_index("ix_feedback_inference_jobs_user_id", table_name="feedback_inference_jobs")
    op.drop_table("feedback_inference_jobs")
