"""Add PostgreSQL full-text and vector indexes for memory/history retrieval."""

from alembic import op

revision = "0020_memory_search_indexes"
down_revision = "0019_memory_content_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_content_fts "
        "ON messages USING GIN (to_tsvector('simple', content))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_content_fts "
        "ON memories USING GIN (to_tsvector('simple', content))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
        "ON memories USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_memories_content_fts")
    op.execute("DROP INDEX IF EXISTS ix_messages_content_fts")
