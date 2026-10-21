"""Add durable per-user memory content hashes and uniqueness."""

from hashlib import sha256

import sqlalchemy as sa

from alembic import op

revision = "0019_memory_content_hash"
down_revision = "0018_taste_profile_version"
branch_labels = None
depends_on = None


def _normalize(content: str) -> str:
    return " ".join((content or "").casefold().split())


def _hash(content: str) -> str:
    return sha256(_normalize(content).encode("utf-8")).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("memories")}

    if "content_hash" not in columns:
        op.add_column("memories", sa.Column("content_hash", sa.String(64), nullable=True))

    memory_table = sa.table(
        "memories",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("content", sa.Text),
        sa.column("content_hash", sa.String(64)),
    )

    rows = bind.execute(
        sa.select(
            memory_table.c.id,
            memory_table.c.content,
        ).where(memory_table.c.content_hash.is_(None))
    ).fetchmany(1000)
    while rows:
        for row in rows:
            bind.execute(
                sa.update(memory_table)
                .where(memory_table.c.id == row.id)
                .values(content_hash=_hash(row.content))
            )
        rows = bind.execute(
            sa.select(memory_table.c.id, memory_table.c.content)
            .where(memory_table.c.content_hash.is_(None))
            .limit(1000)
        ).fetchmany(1000)

    # Keep the most important/latest copy when legacy duplicate memories exist.
    bind.execute(sa.text("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, content_hash
                           ORDER BY importance DESC, created_at DESC, id DESC
                       ) AS rn
                FROM memories
            )
            DELETE FROM memories
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """))

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("memories") as batch:
            batch.alter_column("content_hash", existing_type=sa.String(64), nullable=False)
            batch.create_unique_constraint(
                "uq_memories_user_content_hash",
                ["user_id", "content_hash"],
            )
    else:
        op.alter_column("memories", "content_hash", existing_type=sa.String(64), nullable=False)
        op.create_unique_constraint(
            "uq_memories_user_content_hash",
            "memories",
            ["user_id", "content_hash"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("memories")}
    if "uq_memories_user_content_hash" in constraints:
        op.drop_constraint("uq_memories_user_content_hash", "memories", type_="unique")
    if "content_hash" in {column["name"] for column in inspector.get_columns("memories")}:
        op.drop_column("memories", "content_hash")
