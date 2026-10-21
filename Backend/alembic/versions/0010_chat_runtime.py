"""add durable chat message lifecycle and conversation controls"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0010_chat_runtime"
down_revision = "0009_message_history_index"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing_columns = {column["name"]: column for column in sa.inspect(connection).get_columns("messages")}
    message_columns = (
        sa.Column("uuid_id", sa.String(length=36), nullable=True),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("client_message_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="complete"),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.String(length=8), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
    )
    with op.batch_alter_table("messages") as batch:
        for column in message_columns:
            if column.name not in existing_columns:
                batch.add_column(column)

    for (message_id,) in connection.execute(sa.text("SELECT id FROM messages WHERE uuid_id IS NULL")):
        connection.execute(
            sa.text("UPDATE messages SET uuid_id = :uuid_id WHERE id = :message_id"),
            {"uuid_id": str(uuid.uuid4()), "message_id": message_id},
        )

    unique_constraints = {
        constraint["name"] for constraint in sa.inspect(connection).get_unique_constraints("messages")
    }
    indexes = {index["name"] for index in sa.inspect(connection).get_indexes("messages")}
    uuid_nullable = sa.inspect(connection).get_columns("messages")
    uuid_is_nullable = next(column["nullable"] for column in uuid_nullable if column["name"] == "uuid_id")
    with op.batch_alter_table("messages") as batch:
        if uuid_is_nullable:
            batch.alter_column("uuid_id", existing_type=sa.String(length=36), nullable=False)
        if "uq_messages_uuid_id" not in unique_constraints:
            batch.create_unique_constraint("uq_messages_uuid_id", ["uuid_id"])
        if "uq_messages_user_client_id" not in unique_constraints:
            batch.create_unique_constraint("uq_messages_user_client_id", ["user_id", "client_message_id"])
        if "ix_messages_parent_id" not in indexes:
            batch.create_index("ix_messages_parent_id", ["parent_id"])

    conversation_columns = {column["name"] for column in sa.inspect(connection).get_columns("conversations")}
    with op.batch_alter_table("conversations") as batch:
        for name in ("is_pinned", "is_archived", "title_user_set"):
            if name not in conversation_columns:
                batch.add_column(sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("title_user_set")
        batch.drop_column("is_archived")
        batch.drop_column("is_pinned")
    with op.batch_alter_table("messages") as batch:
        batch.drop_index("ix_messages_parent_id")
        batch.drop_constraint("uq_messages_user_client_id", type_="unique")
        batch.drop_constraint("uq_messages_uuid_id", type_="unique")
        batch.drop_column("error_code")
        batch.drop_column("feedback")
        batch.drop_column("latency_ms")
        batch.drop_column("model")
        batch.drop_column("status")
        batch.drop_column("client_message_id")
        batch.drop_column("parent_id")
        batch.drop_column("uuid_id")
