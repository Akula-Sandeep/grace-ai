"""store provider and usage metadata on assistant messages"""

import sqlalchemy as sa

from alembic import op

revision = "0013_message_inference_metadata"
down_revision = "0012_case_insensitive_usernames"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "provider" not in columns:
        op.add_column("messages", sa.Column("provider", sa.String(length=40), nullable=True))
    if "usage_json" not in columns:
        op.add_column("messages", sa.Column("usage_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "usage_json" in columns:
        op.drop_column("messages", "usage_json")
    if "provider" in columns:
        op.drop_column("messages", "provider")
