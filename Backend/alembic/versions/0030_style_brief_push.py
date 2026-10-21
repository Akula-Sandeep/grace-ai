"""Answer style, the daily brief and web push.

``user_personalization_settings.answer_style``: how long answers should be by default
(brief, balanced, detailed); the request in hand always wins.

``user_personalization_settings.daily_brief_enabled``: one short digest a day in the
notification centre (off by default).

``push_subscriptions``: the browser endpoints a user allowed Grace to notify. A row
is removed when the browser says it is gone (HTTP 404/410).

``notifications.pushed_at``: when a notification was sent to the user's devices, so it
is pushed once.
"""

import sqlalchemy as sa

from alembic import op

revision = "0030_style_brief_push"
down_revision = "0029_projects_sharing_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_personalization_settings",
        sa.Column(
            "answer_style",
            sa.String(length=16),
            nullable=False,
            server_default="balanced",
        ),
    )
    op.add_column(
        "user_personalization_settings",
        sa.Column(
            "daily_brief_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(length=1000), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(length=200), nullable=False),
        sa.Column("auth", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])
    op.add_column(
        "notifications",
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "pushed_at")
    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
    op.drop_column("user_personalization_settings", "daily_brief_enabled")
    op.drop_column("user_personalization_settings", "answer_style")
