"""Record generated result application separately for each author's copy."""
from alembic import op
import sqlalchemy as sa

revision = "d926b850ac71"
down_revision = "b582d81cb620"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("job_applications",
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("applied_at", sa.Integer(), nullable=False),
        sa.Column("workspace_version", sa.Integer(), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False))


def downgrade():
    op.drop_table("job_applications")
