"""Add durable user taste-profile cache versioning."""

import sqlalchemy as sa

from alembic import op

revision = "0018_taste_profile_version"
down_revision = "0017_knowledge_content_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "taste_profile_version" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "taste_profile_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "taste_profile_version" in columns:
        op.drop_column("users", "taste_profile_version")
