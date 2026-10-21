"""track OTP lifecycle and permit multiple issued codes per email"""

import sqlalchemy as sa

from alembic import op

revision = "0005_otp_lifecycle"
down_revision = "0004_email_otp_auth"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("otp_verifications")}
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "otp_verifications",
            recreate="always",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch:
            if "is_used" not in columns:
                batch.add_column(
                    sa.Column(
                        "is_used",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    )
                )
            if "created_at" not in columns:
                batch.add_column(
                    sa.Column(
                        "created_at",
                        sa.DateTime(timezone=True),
                        nullable=False,
                        server_default=sa.func.now(),
                    )
                )
            for constraint in inspector.get_unique_constraints("otp_verifications"):
                name = constraint["name"] or "uq_otp_verifications_email"
                batch.drop_constraint(name, type_="unique")
            for index in inspector.get_indexes("otp_verifications"):
                if index["name"] == "ix_otp_verifications_email":
                    batch.drop_index(index["name"])
            batch.create_index("ix_otp_verifications_email", ["email"], unique=False)
    else:
        if "is_used" not in columns:
            op.add_column(
                "otp_verifications",
                sa.Column("is_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            )
        if "created_at" not in columns:
            op.add_column(
                "otp_verifications",
                sa.Column(
                    "created_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.func.now(),
                ),
            )
        for constraint in inspector.get_unique_constraints("otp_verifications"):
            if constraint.get("name"):
                op.drop_constraint(constraint["name"], "otp_verifications", type_="unique")
        for index in inspector.get_indexes("otp_verifications"):
            if index["name"] == "ix_otp_verifications_email":
                op.drop_index(index["name"], table_name="otp_verifications")
        op.create_index("ix_otp_verifications_email", "otp_verifications", ["email"], unique=False)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("otp_verifications", recreate="always") as batch:
            batch.drop_index("ix_otp_verifications_email")
            batch.create_unique_constraint("uq_otp_verifications_email", ["email"])
            batch.drop_column("created_at")
            batch.drop_column("is_used")
    else:
        op.drop_index("ix_otp_verifications_email", table_name="otp_verifications")
        op.create_unique_constraint("uq_otp_verifications_email", "otp_verifications", ["email"])
        op.drop_column("otp_verifications", "created_at")
        op.drop_column("otp_verifications", "is_used")
