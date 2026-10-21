"""Separate verification and password-reset OTPs."""

import sqlalchemy as sa

from alembic import op

revision = "0015_otp_purposes"
down_revision = "0014_auth_session_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("otp_verifications")}
    if "purpose" not in columns:
        op.add_column(
            "otp_verifications",
            sa.Column("purpose", sa.String(32), nullable=False, server_default="verification"),
        )
    op.create_index(
        "ix_otp_verifications_email_purpose_active",
        "otp_verifications",
        ["email", "purpose"],
        unique=True,
        sqlite_where=sa.text("is_used = 0"),
        postgresql_where=sa.text("is_used = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_otp_verifications_email_purpose_active", table_name="otp_verifications")
    op.drop_column("otp_verifications", "purpose")
