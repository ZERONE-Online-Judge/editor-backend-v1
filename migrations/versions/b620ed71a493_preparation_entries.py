"""Keep successful preparation stages separate from measured judging results."""
from alembic import op
import sqlalchemy as sa

revision = "b620ed71a493"
down_revision = "f39a852c107d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("preparation_entries",
        sa.Column("problem_id", sa.String(32), sa.ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False))
    op.create_index("ix_preparation_entries_expires_at", "preparation_entries", ["expires_at"])


def downgrade():
    op.drop_table("preparation_entries")
