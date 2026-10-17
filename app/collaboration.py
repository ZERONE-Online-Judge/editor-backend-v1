from fastapi import APIRouter, Depends
from pydantic import Field
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import current_user
from .db import Access, Collection, Issue, Problem, User, now, session_dependency
from .domain import authorize, fail
from .folder_access import effective_access, can_manage_access
from .problems import problem_json
from .permissions import Permission, PROBLEM, grants, has, delegate, stored, projection
from .schema import Strict
from .access_mail import enqueue as enqueue_access_mail
from .folders import lock_user

router = APIRouter(prefix="/api", tags=["collaboration"])


@router.get("/problems/{problem_id}/accesses")
def accesses(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user, permission="manage_access")
    rows = db.scalars(select(Access).where(Access.problem_id == problem_id)).all()
    return [{"user_id": a.user_id, "handle": db.get(User, a.user_id).handle, "display_name": db.get(User, a.user_id).display_name, "role": a.role, "reviewer": a.reviewer, "translator": a.translator, "permissions": sorted(grants(a))} for a in rows]


class SetAccess(Strict):
    login: str = Field(min_length=1, max_length=254)
    role: Literal["READ", "WRITE", "NONE"] = "READ"
    permissions: list[Permission] | None = Field(default=None, max_length=16)
    reviewer: bool = False
    translator: bool = False


@router.put("/problems/{problem_id}/accesses")
def set_access(problem_id: str, values: SetAccess, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, actor = authorize(db, problem_id, user, permission="manage_access")
    lock_user(db, db.get(User, problem.owner_id))
    problem, actor = authorize(db, problem_id, user, permission="manage_access")
    if not can_manage_access(db, problem, user):
        fail("workspace_access_managed", "공유 폴더의 구성원 권한은 폴더 관리자에게 요청하세요. 문제별 직접 공유는 문제 소유자만 변경할 수 있습니다.", 403)
    target = db.scalar(select(User).where((User.handle == values.login) | (User.email == values.login.lower())))
    if not target:
        fail("user_not_found", "Editor에 먼저 로그인한 사용자의 이메일 또는 아이디를 입력하세요.", 404)
    if target.id == problem.owner_id:
        fail("owner_protected", "소유자의 권한은 제거하거나 변경할 수 없습니다.")
    row = db.get(Access, (problem_id, target.id))
    previous = grants(row)
    new_access = row is None
    requested = set(values.permissions) if values.permissions is not None else grants(values)
    if requested - PROBLEM:
        fail("invalid_permissions", "폴더 전용 권한은 문제에 부여할 수 없습니다.")
    if row:
        delegate(actor, grants(row))
    if values.role == "NONE":
        if row:
            db.delete(row)
    else:
        delegate(actor, requested)
        if not row:
            row = Access(problem_id=problem_id, user_id=target.id)
            db.add(row)
        row.role, row.reviewer, row.translator = projection(requested), "execute" in requested, "translate" in requested
        row.permissions = stored(requested)
        if new_access or requested - previous:
            enqueue_access_mail(db, kind="problem_access", resource_id=problem_id,
                resource_name=problem.document["info"]["name"], email=target.email,
                actor=user, permissions=requested)
    db.commit()
    return {"ok": True}


@router.get("/problems/{problem_id}/issues")
def issues(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    rows = db.scalars(select(Issue).where(Issue.problem_id == problem_id).order_by(Issue.updated_at.desc())).all()
    return [issue_json(db, row) for row in rows]


def issue_json(db, row):
    return {"id": row.id, "title": row.title, "content": row.content, "kind": row.kind, "status": row.status,
            "author": db.get(User, row.author_id).display_name,
            "assignee": db.get(User, row.assignee_id).handle if row.assignee_id else "", "comments": row.comments,
            "created_at": row.created_at, "updated_at": row.updated_at}


def issue_permission(db, problem_id, user):
    _, access = authorize(db, problem_id, user)
    if not has(access, "issues"):
        fail("forbidden", "이슈를 작성할 권한이 없습니다.", 403)


def assignee(db, problem_id, handle):
    if not handle:
        return None
    target = db.scalar(select(User).where(User.handle == handle))
    member = effective_access(db, problem_id, target.id) if target else None
    if not member or not has(member, "issues"):
        fail("invalid_assignee", "이슈 처리 권한이 있는 담당자를 선택하세요.")
    return target.id


class NewIssue(Strict):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=16384)
    kind: Literal["BUG", "DISCUSSION", "ENHANCEMENT"] = "BUG"
    assignee: str = ""


@router.post("/problems/{problem_id}/issues", status_code=201)
def add_issue(problem_id: str, values: NewIssue, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    issue_permission(db, problem_id, user)
    row = Issue(problem_id=problem_id, author_id=user.id, title=values.title, content=values.content, kind=values.kind, assignee_id=assignee(db, problem_id, values.assignee))
    db.add(row)
    db.commit()
    return issue_json(db, row)


class IssueUpdate(Strict):
    comment: str = Field(default="", max_length=16384)
    status: Literal["CLOSED", "REOPENED"] | None = None
    kind: Literal["BUG", "DISCUSSION", "ENHANCEMENT"] | None = None
    assignee: str | None = None


@router.patch("/problems/{problem_id}/issues/{issue_id}")
def update_issue(problem_id: str, issue_id: str, values: IssueUpdate, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    issue_permission(db, problem_id, user)
    row = db.get(Issue, issue_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "이슈를 찾을 수 없습니다.", 404)
    if row.status == "CLOSED" and values.status != "REOPENED":
        fail("closed_issue", "완료된 이슈는 먼저 다시 열어 주세요.")
    if values.status == "REOPENED" and row.status != "CLOSED":
        fail("invalid_transition", "완료된 이슈만 다시 열 수 있습니다.")
    changes = []
    if values.status and values.status != row.status:
        changes.append(f"상태: {row.status} → {values.status}")
        row.status = values.status
    if values.kind and values.kind != row.kind:
        changes.append(f"유형: {row.kind} → {values.kind}")
        row.kind = values.kind
    if values.assignee is not None:
        target = assignee(db, problem_id, values.assignee)
        if target != row.assignee_id:
            changes.append(f"담당자: {values.assignee or '해제'}")
            row.assignee_id = target
    if not values.comment.strip() and not changes:
        fail("empty_change", "댓글이나 변경사항이 필요합니다.")
    row.comments = [*row.comments, {"author": user.display_name, "content": values.comment, "changes": changes, "created_at": now()}]
    row.updated_at = now()
    db.commit()
    return issue_json(db, row)


class CollectionValues(Strict):
    title: str = Field(min_length=1, max_length=200)
    problem_ids: list[str] = Field(default_factory=list, max_length=100)


@router.get("/contests")
def list_contests(user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    return [{"id": row.id, "title": row.title, "problem_ids": row.problem_ids, "created_at": row.created_at} for row in db.scalars(select(Collection).where(Collection.owner_id == user.id).order_by(Collection.created_at.desc()))]


@router.post("/contests", status_code=201)
def create_contest(values: CollectionValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    for problem_id in values.problem_ids:
        authorize(db, problem_id, user)
    row = Collection(owner_id=user.id, title=values.title, problem_ids=list(dict.fromkeys(values.problem_ids)))
    db.add(row)
    db.commit()
    return {"id": row.id, "title": row.title, "problem_ids": row.problem_ids}


@router.put("/contests/{contest_id}")
def update_contest(contest_id: str, values: CollectionValues, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = db.get(Collection, contest_id)
    if not row or row.owner_id != user.id:
        fail("not_found", "대회를 찾을 수 없습니다.", 404)
    for problem_id in values.problem_ids:
        authorize(db, problem_id, user)
    row.title, row.problem_ids = values.title, list(dict.fromkeys(values.problem_ids))
    db.commit()
    return {"id": row.id, "title": row.title, "problem_ids": row.problem_ids}
