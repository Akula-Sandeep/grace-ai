"""add email-based OTP authentication"""

import sqlalchemy as sa

from alembic import op

revision = "0004_email_otp_auth"
down_revision = "0003_production_indexes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "email" not in user_columns:
        op.add_column("users", sa.Column("email", sa.String(length=254), nullable=True))
    user_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("users")}
    if "ix_users_email" not in user_indexes:
        op.create_index("ix_users_email", "users", ["email"], unique=True)

    if "otp_verifications" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "otp_verifications",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=254), nullable=False),
            sa.Column("otp_hash", sa.String(length=255), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email"),
        )
    otp_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("otp_verifications")}
    if "ix_otp_verifications_email" not in otp_indexes:
        op.create_index("ix_otp_verifications_email", "otp_verifications", ["email"], unique=True)


def downgrade():
    bind = op.get_bind()
    if "otp_verifications" in sa.inspect(bind).get_table_names():
        otp_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("otp_verifications")}
        if "ix_otp_verifications_email" in otp_indexes:
            op.drop_index("ix_otp_verifications_email", table_name="otp_verifications")
        op.drop_table("otp_verifications")
    user_columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    if "email" in user_columns:
        op.drop_index("ix_users_email", table_name="users")
        op.drop_column("users", "email")
