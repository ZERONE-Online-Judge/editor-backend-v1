"""Queue access notifications in the same transaction as permission grants."""
from alembic import op
import sqlalchemy as sa

revision = "ca94bf710e23"
down_revision = "a83f910b52cd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("access_mail_outbox",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(32), nullable=False),
        sa.Column("resource_name", sa.String(200), nullable=False),
        sa.Column("recipient_email", sa.String(254), nullable=False),
        sa.Column("actor_name", sa.String(80), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("invitation_id", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.Integer(), nullable=False),
        sa.Column("lease", sa.String(32), nullable=True),
        sa.Column("delivery_id", sa.String(36), nullable=True),
        sa.Column("last_error", sa.String(80), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False))
    op.create_index("ix_access_mail_outbox_status", "access_mail_outbox", ["status"])
    op.create_index("ix_access_mail_outbox_available_at", "access_mail_outbox", ["available_at"])


def downgrade():
    op.drop_table("access_mail_outbox")
