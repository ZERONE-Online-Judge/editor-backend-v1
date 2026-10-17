"""Personal filing and shared trees with inherited, revocable permissions."""
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import current_user
from .db import Access, Folder, Placement, Problem, SharedPlacement, SharedWorkspace, User, session_dependency
from .domain import authorize, fail
from .folder_access import workspace_ids, workspace_role, workspace_roles, shared_folder, workspace_permissions
from .schema import Strict

router = APIRouter(prefix="/api/folders", tags=["folders"])


def lock_user(db, user):
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(User).where(User.id == user.id).values(display_name=User.display_name))
    else:
        db.scalar(select(User).where(User.id == user.id).with_for_update())


def owned_folder(db, user, folder_id, write=True, permission="manage_folders"):
    if folder_id is None:
        return None
    folder = db.scalar(select(Folder).where(Folder.id == folder_id).execution_options(populate_existing=True))
    role = workspace_role(db, folder.workspace_id, user.id) if folder and folder.workspace_id else None
    if not folder or (not role if folder.workspace_id else folder.user_id != user.id):
        fail("folder_not_found", "폴더를 찾을 수 없습니다.", 404)
    if write and folder.workspace_id and permission not in workspace_permissions(db, folder.workspace_id, user.id):
        fail("folder_read_only", "이 공유 폴더에서 해당 작업을 할 권한이 없습니다.", 403)
    return folder


def lock_folder(db, user, folder_id, permission="manage_folders"):
    folder = owned_folder(db, user, folder_id, write=False)
    lock_user(db, db.get(User, folder.user_id) if folder else user)
    return owned_folder(db, user, folder_id, permission=permission)


def place(db, user, problem_id, folder_id, permission="manage_folders"):
    folder = lock_folder(db, user, folder_id, permission=permission)
    if folder and folder.workspace_id:
        row = db.get(SharedPlacement, problem_id)
        if row:
            row.folder_id = folder.id
        else:
            db.add(SharedPlacement(problem_id=problem_id, folder_id=folder.id))
        return
    row = db.get(Placement, (user.id, problem_id))
    if row:
        row.folder_id = folder_id
    elif folder_id:
        db.add(Placement(user_id=user.id, problem_id=problem_id, folder_id=folder_id))


def folder_json(row, db=None, user=None, roles=None):
    role = (roles.get(row.workspace_id) if roles is not None else workspace_role(db, row.workspace_id, user.id)) if row.workspace_id and db and user else None
    permissions = workspace_permissions(db, row.workspace_id, user.id) if row.workspace_id else set()
    return {"id": row.id, "name": row.name, "parent_id": row.parent_id,
            "workspace_id": row.workspace_id, "workspace_role": role,
            "permissions": sorted(permissions),
            "can_edit": not row.workspace_id or "manage_folders" in permissions,
            "can_create": not row.workspace_id or "create_problems" in permissions,
            "can_manage": not row.workspace_id or "manage_access" in permissions}


def check_parent(db, user, parent_id, moving_id=None):
    current = owned_folder(db, user, parent_id)
    depth = 0
    while current:
        if current.id == moving_id:
            fail("folder_cycle", "폴더를 자기 자신이나 하위 폴더로 옮길 수 없습니다.")
        depth += 1
        if depth >= 12:
            fail("folder_depth", "폴더는 최대 12단계까지 만들 수 있습니다.")
        current = owned_folder(db, user, current.parent_id)
    return depth


def commit(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail("folder_name_taken", "이 위치에 같은 이름의 폴더가 있습니다.", 409)


class FolderValues(Strict):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str | None = Field(default=None, min_length=32, max_length=32)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or value in {".", ".."} or any(ord(c) < 32 or c in "/\\" for c in value):
            raise ValueError("Use a nonempty folder name without path separators")
        return value


def accessible_folders(db, user):
    return db.scalars(select(Folder).where(or_(
        (Folder.user_id == user.id) & Folder.workspace_id.is_(None),
        Folder.workspace_id.in_(workspace_ids(db, user.id)))).order_by(Folder.name_key)).all()


@router.get("")
def list_folders(user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    roles = workspace_roles(db, user.id)
    return [folder_json(row, db, user, roles) for row in accessible_folders(db, user)]


@router.post("", status_code=201)
def create_folder(values: FolderValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    parent = lock_folder(db, user, values.parent_id)
    check_parent(db, user, values.parent_id)
    owner_id = parent.user_id if parent else user.id
    if len(db.scalars(select(Folder.id).where(Folder.user_id == owner_id)).all()) >= 200:
        fail("too_many_folders", "소유자 계정당 폴더는 200개까지 만들 수 있습니다.")
    row = Folder(user_id=owner_id, workspace_id=parent.workspace_id if parent else None,
                 name=values.name, name_key=values.name.casefold(), parent_id=values.parent_id, parent_key=values.parent_id or "")
    db.add(row)
    commit(db)
    return folder_json(row, db, user)


@router.put("/{folder_id}")
def edit_folder(folder_id: str, values: FolderValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = lock_folder(db, user, folder_id)
    parent = owned_folder(db, user, values.parent_id)
    if row.workspace_id and row.id == row.workspace_id:
        if values.parent_id is not None or "manage_access" not in workspace_permissions(db, row.workspace_id, user.id):
            fail("workspace_root", "공유 폴더의 최상위 이름은 관리자만 변경할 수 있고 다른 폴더로 이동할 수 없습니다.", 403)
    elif row.workspace_id != (parent.workspace_id if parent else None):
        fail("folder_scope", "폴더는 같은 개인 영역 또는 같은 공유 폴더 안에서만 이동할 수 있습니다.")
    depth = check_parent(db, user, values.parent_id, folder_id)
    frontier = {folder_id}
    rows = db.scalars(select(Folder).where(Folder.user_id == row.user_id)).all()
    while frontier:
        frontier = {f.id for f in rows if f.parent_id in frontier}
        if frontier:
            depth += 1
    if depth + 1 > 12:
        fail("folder_depth", "하위 폴더를 포함해 최대 12단계까지 이동할 수 있습니다.")
    row.name, row.name_key = values.name, values.name.casefold()
    row.parent_id, row.parent_key = values.parent_id, values.parent_id or ""
    commit(db)
    return folder_json(row, db, user)


class MoveProblems(Strict):
    problem_ids: list[str] = Field(min_length=1, max_length=500)
    folder_id: str | None = Field(default=None, min_length=32, max_length=32)
    share_confirmed: bool = False


@router.post("/move-problems")
def move_problems(values: MoveProblems, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    destination = owned_folder(db, user, values.folder_id)
    ids = set(values.problem_ids)
    # Serialize moves of the same problem before taking tree locks. Otherwise
    # another move could change the source scope while we wait for its owner.
    db.scalars(select(Problem).where(Problem.id.in_(ids)).order_by(Problem.id).with_for_update()).all()
    # Lock every affected tree in stable order before checking current membership.
    sources = [shared_folder(db, pid) for pid in ids]
    owners = {user.id, *[f.user_id for f in [destination, *sources] if f]}
    for owner_id in sorted(owners):
        lock_user(db, db.get(User, owner_id))
    destination = owned_folder(db, user, values.folder_id)
    for pid in ids:
        problem, _ = authorize(db, pid, user)
        source = shared_folder(db, pid)
        target_scope = destination.workspace_id if destination else None
        source_scope = source.workspace_id if source else None
        if source_scope != target_scope and (source_scope or target_scope):
            if problem.owner_id != user.id:
                fail("share_owner_required", "공유 범위를 바꾸는 이동은 문제 소유자만 할 수 있습니다.", 403)
            if not values.share_confirmed:
                fail("share_confirmation_required", "이동하면 공유 폴더 구성원의 문제 접근 권한이 바뀝니다. 확인 후 이동하세요.", 409)
        elif source_scope:
            owned_folder(db, user, source.id)
    for pid in ids:
        if not destination or not destination.workspace_id:
            previous = db.get(SharedPlacement, pid)
            if previous:
                db.delete(previous)
        place(db, user, pid, values.folder_id)
    db.commit()
    return {"moved": len(ids), "folder_id": values.folder_id}


@router.delete("/{folder_id}")
def delete_folder(folder_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = lock_folder(db, user, folder_id)
    children = db.scalars(select(Folder).where(Folder.parent_id == row.id)).all()
    if row.workspace_id:
        placements = db.scalars(select(SharedPlacement).where(SharedPlacement.folder_id == row.id)).all()
        if row.id == row.workspace_id:
            if workspace_role(db, row.workspace_id, user.id) != "OWNER":
                fail("owner_required", "공유 폴더 소유자만 삭제할 수 있습니다.", 403)
            if children or placements:
                fail("workspace_not_empty", "하위 폴더와 문제를 모두 옮긴 뒤 공유 폴더를 삭제하세요.", 409)
    else:
        placements = db.scalars(select(Placement).where(Placement.user_id == user.id, Placement.folder_id == row.id)).all()
    for child in children:
        child.parent_id, child.parent_key = row.parent_id, row.parent_id or ""
    for item in placements:
        item.folder_id = row.parent_id
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        fail("folder_name_taken", "상위 위치에 같은 이름의 하위 폴더가 있습니다. 이름을 바꾼 뒤 삭제하세요.", 409)
    root = db.get(SharedWorkspace, row.workspace_id) if row.id == row.workspace_id else None
    db.delete(row)
    db.flush()
    if root:
        db.delete(root)
    commit(db)
    return {"deleted": True, "parent_id": row.parent_id}
