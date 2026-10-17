"""Derived folder grants never become permanent problem ACL entries."""
from types import SimpleNamespace
from sqlalchemy import select, or_
from .permissions import grants, stored, projection, PROBLEM, EDIT
from .db import Access, Folder, SharedPlacement, SharedWorkspace, WorkspaceMember

WRITE_ROLES = {"OWNER", "ADMIN", "EDITOR"}
ADMIN_ROLES = {"OWNER", "ADMIN"}


def workspace_role(db, workspace_id, user_id):
    owner_id = db.scalar(select(SharedWorkspace.owner_id).where(SharedWorkspace.id == workspace_id))
    if not owner_id:
        return None
    if owner_id == user_id:
        return "OWNER"
    # Re-read after a lock or permission change, including within one request.
    return db.scalar(select(WorkspaceMember.role).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id))


def workspace_ids(db, user_id):
    return select(SharedWorkspace.id).where(or_(SharedWorkspace.owner_id == user_id,
        SharedWorkspace.id.in_(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id))))


def shared_folder(db, problem_id):
    return db.scalar(select(Folder).join(SharedPlacement, SharedPlacement.folder_id == Folder.id)
                     .where(SharedPlacement.problem_id == problem_id))


def workspace_access(db, workspace_id, user_id):
    role = workspace_role(db, workspace_id, user_id)
    if not role:
        return None
    if role == "OWNER":
        return SimpleNamespace(role=role, permissions=None)
    return db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user_id).execution_options(populate_existing=True))


def workspace_permissions(db, workspace_id, user_id):
    return grants(workspace_access(db, workspace_id, user_id), folder=True)


def effective_access(db, problem_id, user_id):
    direct = db.scalar(select(Access).where(Access.problem_id == problem_id, Access.user_id == user_id)
                       .execution_options(populate_existing=True))
    folder = shared_folder(db, problem_id)
    inherited = workspace_access(db, folder.workspace_id, user_id) if folder else None
    return merge_access(direct, inherited)


def merge_access(direct, inherited):
    if not direct and not inherited:
        return None
    values = grants(direct) | (grants(inherited, folder=True) & PROBLEM)
    role = "OWNER" if direct and direct.role == "OWNER" else projection(values)
    inherited_role = inherited if isinstance(inherited, str) else getattr(inherited, "role", None)
    return SimpleNamespace(role=role, permissions=stored(values),
        reviewer=bool(direct and direct.reviewer or inherited_role == "REVIEWER" or inherited_role == "CUSTOM" and "execute" in values and not EDIT <= values),
        translator=bool(direct and direct.translator or inherited_role == "TRANSLATOR" or inherited_role == "CUSTOM" and "translate" in values))


def visible_problem_ids(db, user_id):
    return select(SharedPlacement.problem_id).join(Folder, Folder.id == SharedPlacement.folder_id).where(
        Folder.workspace_id.in_(workspace_ids(db, user_id)))


def writable_problem_ids(user_id):
    # Publishing is separate from editing. JSON scalar access is portable to
    # PostgreSQL and SQLite; NULL rows retain their exact legacy role behavior.
    def can_publish(model, roles):
        return or_(model.permissions["publish"].as_boolean().is_(True),
                   model.permissions.is_(None) & model.role.in_(roles))
    direct = select(Access.problem_id).where(Access.user_id == user_id,
        or_(Access.role == "OWNER", can_publish(Access, {"WRITE"})))
    spaces = select(SharedWorkspace.id).where(or_(SharedWorkspace.owner_id == user_id,
        SharedWorkspace.id.in_(select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user_id, can_publish(WorkspaceMember, WRITE_ROLES)))))
    inherited = select(SharedPlacement.problem_id).join(Folder, Folder.id == SharedPlacement.folder_id).where(Folder.workspace_id.in_(spaces))
    return direct.union(inherited)


def can_manage_access(db, problem, user):
    # Shared editors cannot turn a revocable folder grant into permanent access.
    return problem.owner_id == user.id or shared_folder(db, problem.id) is None


def workspace_roles(db, user_id):
    roles = dict(db.execute(select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(WorkspaceMember.user_id == user_id)).all())
    roles.update({ident: "OWNER" for ident in db.scalars(select(SharedWorkspace.id).where(SharedWorkspace.owner_id == user_id))})
    return roles


def problem_access_context(db, problem_ids, user_id):
    direct = {a.problem_id: a for a in db.scalars(select(Access).where(Access.user_id == user_id, Access.problem_id.in_(problem_ids)))}
    shared = dict(db.execute(select(SharedPlacement.problem_id, Folder).join(Folder, Folder.id == SharedPlacement.folder_id).where(SharedPlacement.problem_id.in_(problem_ids))).all())
    roles = {m.workspace_id: m for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user_id))}
    roles.update({ident: SimpleNamespace(role="OWNER", permissions=None) for ident in db.scalars(select(SharedWorkspace.id).where(SharedWorkspace.owner_id == user_id))})
    roots = dict(db.execute(select(Folder.id, Folder.name).where(Folder.id.in_(roles))).all())
    return direct, shared, roles, roots
