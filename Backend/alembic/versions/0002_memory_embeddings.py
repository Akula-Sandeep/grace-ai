"""add memory embeddings for semantic recall"""

import sqlalchemy as sa

from alembic import op

revision = "0002_memory_embeddings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cols = {c["name"] for c in sa.inspect(bind).get_columns("memories")}
    if "embedding" not in cols:
        if bind.dialect.name == "postgresql":
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            op.execute("ALTER TABLE memories ADD COLUMN embedding vector(384)")
        else:
            op.add_column("memories", sa.Column("embedding", sa.JSON(), nullable=True))


def downgrade():
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("memories")}
    if "embedding" in cols:
        op.drop_column("memories", "embedding")
