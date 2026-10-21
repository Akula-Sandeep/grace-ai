"""Add durable guest-state adoption idempotency."""

import sqlalchemy as sa

from alembic import op

revision = "0023_guest_adoption_idempotency"
down_revision = "0022_lifecycle_jobs_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guest_adoptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("guest_session_id", sa.String(length=36), nullable=False),
        sa.Column("watchlist_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("watched_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "guest_session_id", name="uq_guest_adoptions_user_session"),
    )
    op.create_index("ix_guest_adoptions_user_id", "guest_adoptions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_guest_adoptions_user_id", table_name="guest_adoptions")
    op.drop_table("guest_adoptions")
