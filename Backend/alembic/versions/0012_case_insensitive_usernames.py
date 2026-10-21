"""enforce case-insensitive username uniqueness"""

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from alembic import op

revision = "0012_case_insensitive_usernames"
down_revision = "0011_conversation_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT lower(username), count(*) FROM users "
            "GROUP BY lower(username) HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates is not None:
        raise RuntimeError(
            "Cannot enforce case-insensitive usernames: duplicate case-folded usernames exist. "
            "Resolve duplicates before retrying this migration."
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("users")}
    if "uq_users_username_lower" not in existing:
        try:
            op.create_index(
                "uq_users_username_lower",
                "users",
                [sa.text("lower(username)")],
                unique=True,
            )
        except OperationalError as exc:
            if "already exists" not in str(exc).casefold():
                raise


def downgrade() -> None:
    op.drop_index("uq_users_username_lower", table_name="users")
