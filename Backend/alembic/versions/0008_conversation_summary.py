"""add rolling conversation summaries and chronological message index"""

import sqlalchemy as sa

from alembic import op

revision = "0008_conversation_summary"
down_revision = "0007_conversation_soft_deletes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("conversations")}
    if "summary" not in columns:
        op.add_column(
            "conversations",
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        )
    if "summary_through_message_id" not in columns:
        op.add_column(
            "conversations",
            sa.Column("summary_through_message_id", sa.Integer(), nullable=True),
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("messages")}
    index_name = "ix_messages_conversation_id_created_at"
    if index_name not in indexes:
        op.create_index(index_name, "messages", ["conversation_id", "created_at"])


def downgrade():
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("messages")}
    index_name = "ix_messages_conversation_id_created_at"
    if index_name in indexes:
        op.drop_index(index_name, table_name="messages")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("conversations")}
    if "summary_through_message_id" in columns:
        op.drop_column("conversations", "summary_through_message_id")
    if "summary" in columns:
        op.drop_column("conversations", "summary")
