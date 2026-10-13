"""Keep shared invocation review comments separate from worker results."""
from alembic import op
import sqlalchemy as sa

revision = "e72b134fa890"
down_revision = "d926b850ac71"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("job_comments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False))
    op.create_index("ix_job_comments_job_id", "job_comments", ["job_id"])


def downgrade():
    op.drop_table("job_comments")
