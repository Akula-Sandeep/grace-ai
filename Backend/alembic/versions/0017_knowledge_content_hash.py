"""Deduplicate ingested knowledge documents."""

import sqlalchemy as sa

from alembic import op

revision = "0017_knowledge_content_hash"
down_revision = "0016_security_audit_and_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("knowledge_documents")}
    if "content_hash" not in columns:
        op.add_column(
            "knowledge_documents",
            sa.Column("content_hash", sa.String(64), nullable=True),
        )
        op.create_index(
            "ix_knowledge_documents_content_hash",
            "knowledge_documents",
            ["content_hash"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ix_knowledge_documents_content_hash" in {
        index["name"] for index in inspector.get_indexes("knowledge_documents")
    }:
        op.drop_index("ix_knowledge_documents_content_hash", table_name="knowledge_documents")
    if "content_hash" in {column["name"] for column in inspector.get_columns("knowledge_documents")}:
        op.drop_column("knowledge_documents", "content_hash")
