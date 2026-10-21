"""Add a free-form display name, used for the greeting and account menu.

``username`` is a unique, restricted handle (often auto-generated, e.g.
``user_3fa2...``); it is not something to greet a person with.
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_user_display_name"
down_revision = "0026_notification_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "display_name")
