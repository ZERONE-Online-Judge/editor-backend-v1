"""Personal organization, independent of shared problem permissions/revisions."""
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import current_user
from .db import Access, Folder, Placement, Problem, User, session_dependency
from .domain import fail
from .schema import Strict

router = APIRouter(prefix="/api/folders", tags=["folders"])


def owned_folder(db, user, folder_id):
    if folder_id is None:
        return None
    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        fail("folder_not_found", "폴더를 찾을 수 없습니다.", 404)
    return folder


def place(db, user, problem_id, folder_id):
    row = db.get(Placement, (user.id, problem_id))
    if row:
        row.folder_id = folder_id
    elif folder_id:
        db.add(Placement(user_id=user.id, problem_id=problem_id, folder_id=folder_id))


def lock_user(db, user):
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def folder_json(row):
    return {"id": row.id, "name": row.name, "parent_id": row.parent_id}


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


@router.get("")
def list_folders(user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    return [folder_json(row) for row in db.scalars(select(Folder).where(Folder.user_id == user.id).order_by(Folder.name_key))]


@router.post("", status_code=201)
def create_folder(values: FolderValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    lock_user(db, user)
    check_parent(db, user, values.parent_id)
    if len(db.scalars(select(Folder.id).where(Folder.user_id == user.id)).all()) >= 200:
        fail("too_many_folders", "폴더는 계정당 200개까지 만들 수 있습니다.")
    row = Folder(user_id=user.id, name=values.name, name_key=values.name.casefold(), parent_id=values.parent_id, parent_key=values.parent_id or "")
    db.add(row)
    commit(db)
    return folder_json(row)


@router.put("/{folder_id}")
def edit_folder(folder_id: str, values: FolderValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    lock_user(db, user)
    row = owned_folder(db, user, folder_id)
    depth = check_parent(db, user, values.parent_id, folder_id)
    descendants = {folder_id}
    frontier = {folder_id}
    rows = db.scalars(select(Folder).where(Folder.user_id == user.id)).all()
    while frontier:
        frontier = {f.id for f in rows if f.parent_id in frontier}
        descendants.update(frontier)
        if frontier:
            depth += 1
    if depth + 1 > 12:
        fail("folder_depth", "하위 폴더를 포함해 최대 12단계까지 이동할 수 있습니다.")
    row.name, row.name_key = values.name, values.name.casefold()
    row.parent_id, row.parent_key = values.parent_id, values.parent_id or ""
    commit(db)
    return folder_json(row)


class MoveProblems(Strict):
    problem_ids: list[str] = Field(min_length=1, max_length=500)
    folder_id: str | None = Field(default=None, min_length=32, max_length=32)


@router.post("/move-problems")
def move_problems(values: MoveProblems, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    lock_user(db, user)
    owned_folder(db, user, values.folder_id)
    ids = set(values.problem_ids)
    allowed = set(db.scalars(select(Problem.id).join(Access).where(Access.user_id == user.id, Problem.id.in_(ids), Problem.deleted.is_(False))).all())
    if allowed != ids:
        fail("problem_not_found", "접근할 수 없는 문제 또는 휴지통의 문제는 옮길 수 없습니다.", 404)
    for pid in ids:
        place(db, user, pid, values.folder_id)
    db.commit()
    return {"moved": len(ids), "folder_id": values.folder_id}


@router.delete("/{folder_id}")
def delete_folder(folder_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    lock_user(db, user)
    row = owned_folder(db, user, folder_id)
    children = db.scalars(select(Folder).where(Folder.parent_id == row.id, Folder.user_id == user.id)).all()
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
    db.delete(row)
    commit(db)
    return {"deleted": True, "parent_id": row.parent_id}
