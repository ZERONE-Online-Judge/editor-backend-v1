"""Account-bound invitations and workspace membership administration."""
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .auth import current_user
from .db import Folder, SharedWorkspace, WorkspaceMember, WorkspaceInvitation, User, now, uid, session_dependency
from .domain import fail
from .folder_access import workspace_role, workspace_access, workspace_permissions
from .permissions import Permission, grants, delegate, stored
from .folders import FolderValues, lock_user, commit, folder_json
from .schema import Strict

router = APIRouter(prefix="/api/shared-folders", tags=["shared-folders"])
Role = Literal["ADMIN", "EDITOR", "REVIEWER", "TRANSLATOR", "VIEWER"]


def space_access(db, user, workspace_id, admin=False, lock=False):
    space = db.get(SharedWorkspace, workspace_id)
    if not space:
        fail("workspace_not_found", "공유 폴더를 찾을 수 없습니다.", 404)
    if lock:
        lock_user(db, db.get(User, space.owner_id))
    role = workspace_role(db, workspace_id, user.id)
    if not role:
        fail("workspace_not_found", "공유 폴더를 찾을 수 없습니다.", 404)
    if admin and "manage_access" not in workspace_permissions(db, workspace_id, user.id):
        fail("workspace_admin_required", "공유 폴더 관리자 권한이 필요합니다.", 403)
    return space, role


@router.post("", status_code=201)
def create(values: FolderValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    lock_user(db, user)
    if values.parent_id:
        fail("workspace_root", "공유 폴더는 독립된 최상위 폴더로 만드세요.")
    if db.scalar(select(func.count()).select_from(Folder).where(Folder.user_id == user.id)) >= 200:
        fail("too_many_folders", "소유자 계정당 폴더는 200개까지 만들 수 있습니다.")
    space = SharedWorkspace(id=uid(), owner_id=user.id)
    db.add(space)
    db.flush()
    folder = Folder(id=space.id, workspace_id=space.id, user_id=user.id,
                    name=values.name, name_key=values.name.casefold(), parent_key="")
    db.add(folder)
    commit(db)
    return folder_json(folder, db, user)


@router.get("/invitations")
def received(user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    rows = db.scalars(select(WorkspaceInvitation).where(WorkspaceInvitation.email == user.email.lower(),
        WorkspaceInvitation.status == "pending", WorkspaceInvitation.expires_at > now()).order_by(WorkspaceInvitation.created_at.desc())).all()
    return [{"id": r.id, "workspace_id": r.workspace_id, "folder_name": db.get(Folder, r.workspace_id).name,
             "role": r.role, "permissions": sorted(grants(r, folder=True)), "inviter": db.get(User, r.inviter_id).display_name, "expires_at": r.expires_at} for r in rows]


def invitation_for_user(db, user, invitation_id):
    row = db.get(WorkspaceInvitation, invitation_id)
    if not row or row.email != user.email.lower():
        fail("invitation_not_found", "초대를 찾을 수 없습니다.", 404)
    space = db.get(SharedWorkspace, row.workspace_id)
    if not space:
        fail("invitation_not_found", "초대를 찾을 수 없습니다.", 404)
    lock_user(db, db.get(User, space.owner_id))
    row = db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.id == invitation_id).execution_options(populate_existing=True))
    if not row:
        fail("invitation_not_found", "초대를 찾을 수 없습니다.", 404)
    if row.status != "pending" or row.expires_at <= now():
        fail("invitation_expired", "만료되거나 이미 처리된 초대입니다.", 409)
    # If an inviter lost administration before acceptance, their old invitation
    # cannot silently grant access later.
    inviter = workspace_access(db, row.workspace_id, row.inviter_id)
    if "manage_access" not in grants(inviter, folder=True) or not grants(row, folder=True) <= grants(inviter, folder=True):
        fail("invitation_revoked", "초대한 사람의 관리 권한이 해제되었습니다. 관리자에게 새 초대를 요청하세요.", 409)
    return row


@router.post("/invitations/{invitation_id}/accept")
def accept(invitation_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = invitation_for_user(db, user, invitation_id)
    if workspace_role(db, row.workspace_id, user.id):
        fail("already_member", "이미 공유 폴더 구성원입니다.", 409)
    if db.scalar(select(func.count()).select_from(WorkspaceMember).where(WorkspaceMember.workspace_id == row.workspace_id)) >= 200:
        fail("too_many_members", "공유 폴더는 최대 200명을 초대할 수 있습니다.")
    db.add(WorkspaceMember(workspace_id=row.workspace_id, user_id=user.id, role=row.role, permissions=row.permissions))
    row.status = "accepted"
    db.commit()
    return {"workspace_id": row.workspace_id}


@router.post("/invitations/{invitation_id}/decline")
def decline(invitation_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = invitation_for_user(db, user, invitation_id)
    row.status = "declined"
    db.commit()
    return {"ok": True}


@router.get("/{workspace_id}/members")
def members(workspace_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space, role = space_access(db, user, workspace_id)
    rows = [(space.owner_id, "OWNER", None), *[(r.user_id, r.role, r) for r in db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id))]]
    actor = workspace_access(db, workspace_id, user.id)
    allowed = grants(actor, folder=True)
    return {"role": role, "permissions": sorted(allowed), "members": [{"user_id": ident, "role": grant,
        "permissions": sorted(grants(row or "OWNER", folder=True)),
        "display_name": db.get(User, ident).display_name, "handle": db.get(User, ident).handle} for ident, grant, row in rows],
        "invitations": [{"id": r.id, "email": r.email, "role": r.role, "permissions": sorted(grants(r, folder=True)), "expires_at": r.expires_at}
            for r in db.scalars(select(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.status == "pending", WorkspaceInvitation.expires_at > now()))] if "manage_access" in allowed else []}


class Invite(Strict):
    email: str = Field(min_length=3, max_length=254)
    role: Role = "VIEWER"
    permissions: list[Permission] | None = Field(default=None, max_length=16)

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        import re
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter an email address")
        return value


@router.post("/{workspace_id}/invitations", status_code=201)
def invite(workspace_id: str, values: Invite, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space_access(db, user, workspace_id, admin=True, lock=True)
    requested = set(values.permissions) if values.permissions is not None else grants(values, folder=True)
    delegate(workspace_access(db, workspace_id, user.id), requested, folder=True)
    target = db.scalar(select(User).where(func.lower(User.email) == values.email))
    if target and workspace_role(db, workspace_id, target.id):
        fail("already_member", "이미 구성원입니다. 구성원 목록에서 권한을 변경하세요.", 409)
    row = db.scalar(select(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id, WorkspaceInvitation.email == values.email))
    if not row and db.scalar(select(func.count()).select_from(WorkspaceInvitation).where(WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.status == "pending", WorkspaceInvitation.expires_at > now())) >= 200:
        fail("too_many_invitations", "대기 중인 초대는 최대 200개입니다.")
    if not row:
        row = WorkspaceInvitation(workspace_id=workspace_id, email=values.email, role=values.role, inviter_id=user.id, expires_at=now()+7*86400)
        db.add(row)
    row.permissions = stored(requested)
    row.role, row.inviter_id, row.status, row.created_at, row.expires_at = "CUSTOM" if values.permissions is not None else values.role, user.id, "pending", now(), now()+7*86400
    db.commit()
    return {"id": row.id, "expires_at": row.expires_at}


class ChangeRole(Strict):
    role: Role = "VIEWER"
    permissions: list[Permission] | None = Field(default=None, max_length=16)


@router.put("/{workspace_id}/invitations/{invitation_id}")
def change_invitation(workspace_id: str, invitation_id: str, values: ChangeRole, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space, actor_role = space_access(db, user, workspace_id, admin=True, lock=True)
    row = db.get(WorkspaceInvitation, invitation_id)
    if not row or row.workspace_id != workspace_id:
        fail("invitation_not_found", "초대를 찾을 수 없습니다.", 404)
    if row.status != "pending" or row.expires_at <= now():
        fail("invitation_processed", "만료되거나 이미 처리된 초대입니다. 수락한 사용자는 구성원 목록에서 권한을 변경하세요.", 409)
    if actor_role != "OWNER" and "manage_access" in grants(row, folder=True):
        fail("owner_required", "관리자 초대 권한은 소유자만 변경할 수 있습니다.", 403)
    actor = workspace_access(db, workspace_id, user.id)
    requested = set(values.permissions) if values.permissions is not None else grants(values, folder=True)
    delegate(actor, grants(row, folder=True), folder=True)
    delegate(actor, requested, folder=True)
    row.role = "CUSTOM" if values.permissions is not None else values.role
    row.permissions = stored(requested)
    # Acceptance rechecks the administrator who approved the current grant.
    # Editing does not renew the invitation or grant membership in advance.
    row.inviter_id = user.id
    db.commit()
    return {"ok": True}


@router.put("/{workspace_id}/members/{user_id}")
def change_role(workspace_id: str, user_id: str, values: ChangeRole, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space, actor_role = space_access(db, user, workspace_id, admin=True, lock=True)
    row = db.get(WorkspaceMember, (workspace_id, user_id))
    if user_id == space.owner_id:
        fail("owner_protected", "소유자 권한은 변경할 수 없습니다.", 403)
    if not row:
        fail("member_not_found", "구성원을 찾을 수 없습니다.", 404)
    if actor_role != "OWNER" and "manage_access" in grants(row, folder=True) and user.id != user_id:
        fail("owner_required", "다른 관리자 권한은 소유자만 변경할 수 있습니다.", 403)
    actor = workspace_access(db, workspace_id, user.id)
    requested = set(values.permissions) if values.permissions is not None else grants(values, folder=True)
    delegate(actor, grants(row, folder=True), folder=True)
    delegate(actor, requested, folder=True)
    row.role = "CUSTOM" if values.permissions is not None else values.role
    row.permissions = stored(requested)
    db.commit()
    return {"ok": True}


@router.delete("/{workspace_id}/members/{user_id}")
def remove_member(workspace_id: str, user_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space, actor_role = space_access(db, user, workspace_id, lock=True)
    row = db.get(WorkspaceMember, (workspace_id, user_id))
    if user_id == space.owner_id:
        fail("owner_protected", "소유자는 공유 폴더에서 나갈 수 없습니다. 빈 폴더를 삭제할 수 있습니다.", 403)
    if user_id != user.id and ("manage_access" not in workspace_permissions(db, workspace_id, user.id) or actor_role != "OWNER" and row and "manage_access" in grants(row, folder=True)):
        fail("workspace_admin_required", "이 구성원을 내보낼 권한이 없습니다.", 403)
    if not row:
        fail("member_not_found", "구성원을 찾을 수 없습니다.", 404)
    if user_id != user.id:
        delegate(workspace_access(db, workspace_id, user.id), grants(row, folder=True), folder=True)
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.delete("/{workspace_id}/invitations/{invitation_id}")
def revoke(workspace_id: str, invitation_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    space_access(db, user, workspace_id, admin=True, lock=True)
    row = db.get(WorkspaceInvitation, invitation_id)
    if not row or row.workspace_id != workspace_id:
        fail("invitation_not_found", "초대를 찾을 수 없습니다.", 404)
    if row.status != "pending":
        fail("invitation_processed", "이미 처리된 초대입니다. 수락한 사용자는 구성원 목록에서 내보내세요.", 409)
    row.status = "revoked"
    db.commit()
    return {"ok": True}
