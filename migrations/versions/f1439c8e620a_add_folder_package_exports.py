"""Persist folder package export snapshots and child job progress."""
from alembic import op
import sqlalchemy as sa

revision = "f1439c8e620a"
down_revision = "d7ac4261b980"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("folder_package_exports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_id", sa.String(32), nullable=False),
        sa.Column("folder_name", sa.String(80), nullable=False),
        sa.Column("include_descendants", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_coordinated_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True))
    for name in ("user_id", "folder_id", "fingerprint", "status"):
        op.create_index("ix_folder_package_exports_" + name, "folder_package_exports", [name])


def downgrade():
    op.drop_table("folder_package_exports")
