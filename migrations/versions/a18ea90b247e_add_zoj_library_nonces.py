"""Protect ZOJ-initiated library requests against replay."""
from alembic import op
import sqlalchemy as sa
revision = "a18ea90b247e"
down_revision = "c48a976ce1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("zoj_library_nonces", sa.Column("nonce", sa.String(32), primary_key=True), sa.Column("expires_at", sa.Integer(), nullable=False))
    op.create_index("ix_zoj_library_nonces_expires_at", "zoj_library_nonces", ["expires_at"])


def downgrade():
    op.drop_table("zoj_library_nonces")
