"""allow legacy guest movie-state rows while retaining user ownership"""

import sqlalchemy as sa

from alembic import op

revision = "0006_nullable_movie_state_owner"
down_revision = "0005_otp_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("watchlist") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("watched") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    with op.batch_alter_table("watchlist") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("watched") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
