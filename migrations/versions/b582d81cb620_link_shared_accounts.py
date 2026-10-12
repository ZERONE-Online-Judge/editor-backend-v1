"""Link existing email-owned projects to the shared ZOJ account."""

from alembic import op
import sqlalchemy as sa

revision = "b582d81cb620"
down_revision = "a18ea90b247e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users", sa.Column("central_account_id", sa.String(36), nullable=True)
    )
    op.create_index(
        "ix_users_central_account_id", "users", ["central_account_id"], unique=True
    )


def downgrade():
    op.drop_index("ix_users_central_account_id", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("central_account_id")
