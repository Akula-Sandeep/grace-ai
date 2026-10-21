"""Durable per-turn working state and an exactly-once ledger for chat actions.

``messages.turn_state``: the working state after an assistant reply (films
shown, focus, pending question, active request). The next turn starts from the
state of its parent message, so context follows the conversation branch
(edits and regenerations included) and survives cache restarts.

``chat_actions``: one row per side effect a request performed, so a retry or
regeneration of the same request reports the original outcome instead of
acting twice.
"""

import sqlalchemy as sa

from alembic import op

revision = "0028_turn_state_action_ledger"
down_revision = "0027_user_display_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("turn_state", sa.JSON(), nullable=True))
    op.create_table(
        "chat_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("action_key", sa.String(length=160), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "request_key", "action_key", name="uq_chat_actions_request"),
    )
    op.create_index("ix_chat_actions_user_created", "chat_actions", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_actions_user_created", table_name="chat_actions")
    op.drop_table("chat_actions")
    op.drop_column("messages", "turn_state")
