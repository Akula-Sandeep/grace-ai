"""Add an in-app deep link to notifications (proactive follow-ups, sequel notices)."""

import sqlalchemy as sa

from alembic import op

revision = "0026_notification_links"
down_revision = "0025_notifications_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("link_path", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "link_path")
