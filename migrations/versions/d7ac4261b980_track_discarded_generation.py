"""Track each author's decision to discard an unapplied generation result."""
from alembic import op
import sqlalchemy as sa

revision = "d7ac4261b980"
down_revision = "ca94bf710e23"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("job_discards",
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("discarded_at", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("job_discards")
