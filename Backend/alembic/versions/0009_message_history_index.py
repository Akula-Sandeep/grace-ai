"""add composite index for conversation history queries"""

from alembic import op

revision = "0009_message_history_index"
down_revision = "0008_conversation_summary"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_convo_deleted_created ON "
        "messages (conversation_id, is_deleted, created_at);"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_messages_convo_deleted_created;")
