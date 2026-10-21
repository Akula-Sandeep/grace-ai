"""Add notifications/reminders and reminder + motion preferences."""

import sqlalchemy as sa

from alembic import op

revision = "0025_notifications_reminders"
down_revision = "0024_message_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="reminder"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("movie_tmdb_id", sa.Integer(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_user_due", "notifications", ["user_id", "due_at"])
    op.add_column(
        "user_personalization_settings",
        sa.Column("reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "user_interface_settings",
        sa.Column("motion", sa.String(length=16), nullable=False, server_default="system"),
    )


def downgrade() -> None:
    op.drop_column("user_interface_settings", "motion")
    op.drop_column("user_personalization_settings", "reminders_enabled")
    op.drop_index("ix_notifications_user_due", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
