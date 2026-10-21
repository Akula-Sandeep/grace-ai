"""add non-destructive conversation and message deletion markers"""

import sqlalchemy as sa

from alembic import op

revision = "0007_conversation_soft_deletes"
down_revision = "0006_nullable_movie_state_owner"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table in ("conversations", "messages"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "is_deleted" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "is_deleted",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )
        if "deleted_at" not in columns:
            op.add_column(
                table,
                sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            )
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        index_name = f"ix_{table}_is_deleted"
        if index_name not in indexes:
            op.create_index(index_name, table, ["is_deleted"])


def downgrade():
    bind = op.get_bind()
    for table in ("messages", "conversations"):
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        index_name = f"ix_{table}_is_deleted"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "deleted_at" in columns:
            op.drop_column(table, "deleted_at")
        if "is_deleted" in columns:
            op.drop_column(table, "is_deleted")
