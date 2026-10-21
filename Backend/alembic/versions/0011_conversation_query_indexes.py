"""ensure stable ordering indexes for conversation and movie-reference queries"""

import sqlalchemy as sa

from alembic import op

revision = "0011_conversation_query_indexes"
down_revision = "0010_chat_runtime"
branch_labels = None
depends_on = None

OWNERSHIP_TABLE = "alembic_0011_index_ownership"
INDEXES = (
    (
        "conversations",
        "ix_conversations_user_visibility_order",
        (
            "user_id",
            "is_deleted",
            "is_archived",
            "is_pinned DESC",
            "updated_at DESC",
            "id DESC",
        ),
    ),
    (
        "messages",
        "ix_messages_convo_deleted_created_id",
        ("conversation_id", "is_deleted", "created_at DESC", "id DESC"),
    ),
    (
        "movie_references",
        "ix_movie_references_user_created_id",
        ("user_id", "created_at DESC", "id DESC"),
    ),
    (
        "movie_references",
        "ix_movie_references_conversation_created_id",
        ("conversation_id", "created_at DESC", "id DESC"),
    ),
)


def _index_shape(index):
    descending = {
        column for column, directions in index.get("column_sorting", {}).items() if "desc" in directions
    }
    return tuple(index["column_names"]), descending


def _expected_shape(columns):
    names = tuple(column.removesuffix(" DESC") for column in columns)
    descending = {column.removesuffix(" DESC") for column in columns if column.endswith(" DESC")}
    return names, descending


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    created_indexes = []
    for table_name, index_name, columns in INDEXES:
        existing = {index["name"]: index for index in inspector.get_indexes(table_name)}
        expected = _expected_shape(columns)
        if index_name in existing:
            actual = _index_shape(existing[index_name])
            sqlite_reflection_omits_sorting = (
                bind.dialect.name == "sqlite" and actual[0] == expected[0] and not actual[1]
            )
            if actual != expected and not sqlite_reflection_omits_sorting:
                raise RuntimeError(
                    f"Existing index {index_name} does not match its declared columns and ordering."
                )
            continue

        op.create_index(
            index_name,
            table_name,
            [sa.text(column) if " " in column else column for column in columns],
        )
        created_indexes.append((table_name, index_name))

    if created_indexes:
        op.create_table(
            OWNERSHIP_TABLE,
            sa.Column("index_name", sa.String(length=128), primary_key=True),
        )
        ownership = sa.table(OWNERSHIP_TABLE, sa.column("index_name", sa.String(length=128)))
        op.bulk_insert(ownership, [{"index_name": index_name} for _, index_name in created_indexes])


def downgrade():
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if OWNERSHIP_TABLE not in table_names:
        return

    ownership = sa.table(
        OWNERSHIP_TABLE,
        sa.column("index_name", sa.String(length=128)),
    )
    owned_indexes = set(bind.execute(sa.select(ownership.c.index_name)).scalars())
    for table_name, index_name, _columns in reversed(INDEXES):
        if index_name in owned_indexes:
            op.drop_index(index_name, table_name=table_name)
    op.drop_table(OWNERSHIP_TABLE)
