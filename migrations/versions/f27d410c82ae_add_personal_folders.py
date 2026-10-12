"""Add personal folders without modifying shared problems."""
from alembic import op
import sqlalchemy as sa

revision = "f27d410c82ae"
down_revision = "7399ded28647"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("folders", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("folders.id", ondelete="SET NULL")),
        sa.Column("parent_key", sa.String(32), nullable=False), sa.Column("name", sa.String(80), nullable=False),
        sa.Column("name_key", sa.String(240), nullable=False), sa.UniqueConstraint("user_id", "parent_key", "name_key"))
    op.create_index("ix_folders_user_id", "folders", ["user_id"])
    op.create_table("problem_placements",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("problem_id", sa.String(32), sa.ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", sa.String(32), sa.ForeignKey("folders.id", ondelete="SET NULL")))
    op.create_index("ix_problem_placements_folder_id", "problem_placements", ["folder_id"])


def downgrade():
    op.drop_table("problem_placements")
    op.drop_table("folders")
