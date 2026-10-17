"""Preserve legacy grants while enabling explicit capability checklists."""
from alembic import op
import sqlalchemy as sa
revision = "a83f910b52cd"
down_revision = "b620ed71a493"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("accesses", "workspace_members", "workspace_invitations"):
        op.add_column(table, sa.Column("permissions", sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    # Old code cannot enforce feature scopes. Downgrade custom grants to read
    # access rather than expanding them back to a broad legacy writer role.
    op.execute("UPDATE accesses SET role='READ', reviewer=false, translator=false "
               "WHERE permissions IS NOT NULL AND role <> 'OWNER'")
    for table in ("workspace_members", "workspace_invitations"):
        op.execute(f"UPDATE {table} SET role='VIEWER' WHERE permissions IS NOT NULL")
    for table in ("workspace_invitations", "workspace_members", "accesses"):
        op.drop_column(table, "permissions")
