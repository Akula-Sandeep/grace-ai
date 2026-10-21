"""Projects, public share links and feedback reasons.

``projects``: named groups of chats with standing instructions; a chat joins one
through ``conversations.project_id`` (cleared if the project is removed).

``shared_links``: a public, read-only link to one branch of a conversation as it
was when shared. Revoking keeps the row (``revoked_at``) so the link stays dead.

``messages.feedback_reason`` / ``feedback_note``: why an answer was not helpful.
"""

import sqlalchemy as sa

from alembic import op

revision = "0029_projects_sharing_feedback"
down_revision = "0028_turn_state_action_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_projects_user_deleted_updated", "projects", ["user_id", "is_deleted", sa.text("updated_at DESC")]
    )
    op.add_column(
        "conversations",
        sa.Column(
            "project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])

    op.create_table(
        "shared_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("through_message_uuid", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_shared_links_conversation", "shared_links", ["conversation_id"])

    op.add_column("messages", sa.Column("feedback_reason", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("feedback_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "feedback_note")
    op.drop_column("messages", "feedback_reason")
    op.drop_index("ix_shared_links_conversation", table_name="shared_links")
    op.drop_table("shared_links")
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    op.drop_column("conversations", "project_id")
    op.drop_index("ix_projects_user_deleted_updated", table_name="projects")
    op.drop_table("projects")
