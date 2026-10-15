"""Add shared folder membership without materializing problem ACL grants."""
from alembic import op
import sqlalchemy as sa

revision = "f39a852c107d"
down_revision = "e72b134fa890"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shared_workspaces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False))
    op.create_index("ix_shared_workspaces_owner_id", "shared_workspaces", ["owner_id"])
    if op.get_bind().dialect.name == "sqlite":
        # Rebuilding folders would fire existing SET NULL foreign keys and
        # destroy personal filing. SQLite supports an inline nullable FK.
        op.execute("ALTER TABLE folders ADD COLUMN workspace_id VARCHAR(32) "
                   "REFERENCES shared_workspaces(id)")
    else:
        op.add_column("folders", sa.Column("workspace_id", sa.String(32),
            sa.ForeignKey("shared_workspaces.id", name="fk_folders_workspace"), nullable=True))
    op.create_index("ix_folders_workspace_id", "folders", ["workspace_id"])
    op.create_table("workspace_members",
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("shared_workspaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False))
    op.create_table("workspace_invitations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("shared_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("inviter_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("workspace_id", "email"))
    op.create_index("ix_workspace_invitations_workspace_id", "workspace_invitations", ["workspace_id"])
    op.create_index("ix_workspace_invitations_email", "workspace_invitations", ["email"])
    op.create_table("shared_placements",
        sa.Column("problem_id", sa.String(32), sa.ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", sa.String(32), sa.ForeignKey("folders.id", ondelete="RESTRICT"), nullable=False))
    op.create_index("ix_shared_placements_folder_id", "shared_placements", ["folder_id"])


def downgrade():
    # Downgrade removes only shared organization and grants, never problem data.
    op.drop_table("shared_placements")
    op.drop_table("workspace_invitations")
    op.drop_table("workspace_members")
    # Preserve personal folders and placements. Shared folders become the
    # owner's personal folders; their names already obey that namespace.
    op.drop_index("ix_folders_workspace_id", table_name="folders")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE folders DROP COLUMN workspace_id")
    else:
        op.drop_constraint("fk_folders_workspace", "folders", type_="foreignkey")
        op.drop_column("folders", "workspace_id")
    op.drop_table("shared_workspaces")
