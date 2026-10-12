"""Keep reusable ZOJ publication receipts."""
from alembic import op
import sqlalchemy as sa
revision = "c48a976ce1f2"
down_revision = "f27d410c82ae"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("zoj_publications", sa.Column("operation_id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("problem_id", sa.String(32), sa.ForeignKey("problems.id"), nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("contest_id", sa.String(36), nullable=False), sa.Column("division_id", sa.String(36), nullable=False),
        sa.Column("problem_code", sa.String(16), nullable=False), sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False))
    op.create_index("ix_zoj_publications_problem_id", "zoj_publications", ["problem_id"])


def downgrade():
    op.drop_table("zoj_publications")
