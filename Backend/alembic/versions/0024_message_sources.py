"""Persist the sources behind each assistant answer."""

import sqlalchemy as sa

from alembic import op

revision = "0024_message_sources"
down_revision = "0023_guest_adoption_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("sources_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "sources_json")
