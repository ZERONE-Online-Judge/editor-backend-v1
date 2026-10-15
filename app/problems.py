import copy
import json
import re
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import current_user, user_json
from .db import Access, Blob, Issue, Job, Placement, Problem, Revision, User, WorkingCopy, now, session_dependency
from .folders import owned_folder, place
from .domain import authorize, cautions, document_hash, fail, references, three_way_merge, validate_document, workspace
from .config import get_settings
from .execution_limits import public_limits
from .schema import Document, Strict, empty_document
from .storage import put_blob, read_blob
from .prepared_results import pending_preparations

router = APIRouter(prefix="/api/problems", tags=["problems"])


def problem_json(db, problem, user):
    access = db.get(Access, (problem.id, user.id))
    working = db.get(WorkingCopy, (problem.id, user.id))
    owner = db.get(User, problem.owner_id)
    placement = db.get(Placement, (user.id, problem.id))
    doc = working.document if working and access.role in {"WRITE", "OWNER"} else problem.document
    return {"id": problem.id, "slug": problem.slug, "name": doc["info"]["name"], "owner": owner.handle,
            "owner_name": owner.display_name, "role": access.role, "reviewer": access.reviewer,
            "translator": access.translator, "revision": problem.revision,
            "working_revision": working.base_revision if working else problem.revision,
            "modified": bool(working and document_hash(working.document) != document_hash(working.base_document)),
            "tags": doc["info"]["tags"], "note": problem.note, "deleted": problem.deleted,
            "created_at": problem.created_at, "updated_at": problem.updated_at,
            "folder_id": placement.folder_id if placement else None}


@router.get("")
def list_problems(deleted: bool = False, q: str = "", user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    rows = db.scalars(select(Problem).join(Access).where(Access.user_id == user.id, Problem.deleted == deleted).order_by(Problem.updated_at.desc())).all()
    result = [problem_json(db, p, user) for p in rows]
    if q:
        result = [p for p in result if q.lower() in " ".join([p["name"], p["slug"], p["owner"], *p["tags"]]).lower()]
    return result


class CreateProblem(Strict):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    folder_id: str | None = Field(default=None, min_length=32, max_length=32)


@router.post("", status_code=201)
def create_problem(values: CreateProblem, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    owned_folder(db, user, values.folder_id)
    if db.scalar(select(Problem).where(Problem.owner_id == user.id, Problem.slug == values.slug)):
        fail("slug_taken", "같은 이름의 문제가 이미 있습니다.", 409)
    problem = Problem(owner_id=user.id, slug=values.slug, document=empty_document(values.name))
    db.add(problem)
    db.flush()
    db.add(Access(problem_id=problem.id, user_id=user.id, role="OWNER"))
    place(db, user, problem.id, values.folder_id)
    db.add(Revision(problem_id=problem.id, number=0, author_id=user.id, message="문제 생성", document=copy.deepcopy(problem.document)))
    db.commit()
    return problem_json(db, problem, user)


@router.get("/{problem_id}")
def get_problem(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    wc = workspace(db, problem, user)
    editable = access.role in {"OWNER", "WRITE"} or access.translator
    doc = wc.document if editable else problem.document
    return {"problem": problem_json(db, problem, user), "document": doc,
            "version": wc.version, "base_revision": wc.base_revision,
            "document_hash": document_hash(doc), "cautions": cautions(doc),
            "limits": public_limits(get_settings()),
            "pending_preparations": pending_preparations(db, problem_id, user.id, document_hash(doc)) if editable else []}


class SaveWorking(Strict):
    version: int = Field(ge=0)
    document: Document


def save_working(db, wc, doc, version):
    if document_hash(doc) == document_hash(wc.document):
        if wc.version != version:
            fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
        return
    result = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == wc.problem_id, WorkingCopy.user_id == wc.user_id, WorkingCopy.version == version).values(document=doc, version=version + 1, updated_at=now()))
    if not result.rowcount:
        db.rollback()
        fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
    db.commit()
    db.refresh(wc)


@router.put("/{problem_id}/working-copy")
def save_document(problem_id: str, values: SaveWorking, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    if access.role not in {"OWNER", "WRITE"} and not access.translator:
        fail("forbidden", "쓰기 권한이 필요합니다.", 403)
    wc = workspace(db, problem, user)
    doc = validate_document(db, problem.id, values.document.model_dump())
    from .statements import fingerprint, statement_fingerprint
    current = fingerprint(doc)
    for language, stamp in doc.get("translation_reviews",{}).items():
        if stamp != wc.document.get("translation_reviews",{}).get(language) and (not current or stamp['source_hash'] != current or stamp['target_hash'] != statement_fingerprint(doc,language) or language == doc.get("translation_source")):
            fail("translation_review_stale", "현재 원문 내용을 기준으로 번역을 검토한 뒤 표시하세요.")
    if access.role == "READ" and access.translator:
        for key in set(doc) | set(wc.document):
            if key not in {"statements", "translation_reviews"} and doc.get(key) != wc.document.get(key):
                fail("translator_scope", "번역자는 지문만 수정할 수 있습니다.", 403)
        basis=wc.document.get("translation_source")
        if basis and doc['statements'].get(basis)!=wc.document['statements'].get(basis):
            fail("translator_scope", "기준 원문은 작성 권한이 있는 협업자가 수정할 수 있습니다.", 403)
    save_working(db, wc, doc, values.version)
    return {"version": wc.version, "document_hash": document_hash(doc), "modified": doc != wc.base_document,
            "cautions": cautions(doc), "pending_preparations": pending_preparations(db, problem_id, user.id, document_hash(doc))}


class WorkingAction(Strict):
    version: int = Field(ge=0)


class Commit(WorkingAction):
    message: str = Field(min_length=1, max_length=1000)
    minor: bool = False
    expected_revision: int | None = Field(default=None, ge=0)


class CommitPreview(WorkingAction):
    document: Document


@router.post("/{problem_id}/commit-preview")
def preview_commit(problem_id: str, values: CommitPreview, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    if access.role not in {"OWNER", "WRITE"} and not access.translator:
        fail("forbidden", "커밋 권한이 필요합니다.", 403)
    wc = workspace(db, problem, user)
    if wc.version != values.version:
        fail("workspace_changed", "최신 작업 사본을 불러오세요.", 409)
    doc = validate_document(db, problem.id, values.document.model_dump())
    if access.role == "READ" and any(doc.get(k) != wc.base_document.get(k) for k in set(doc)|set(wc.base_document) if k not in {"statements", "translation_reviews"}):
        fail("translator_scope", "번역자는 지문 변경만 커밋할 수 있습니다.", 403)
    proposed, conflicts = three_way_merge(wc.base_document, doc, problem.document)
    basis=problem.document.get("translation_source")
    if access.role == "READ" and basis and proposed['statements'].get(basis)!=problem.document['statements'].get(basis):
        fail("translator_scope", "번역자는 기준 원문을 변경할 수 없습니다.", 403)
    return {"revision": problem.revision, "before": problem.document, "after": proposed,
            "conflicts": conflicts}


@router.post("/{problem_id}/commit")
def commit(problem_id: str, values: Commit, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    if access.role not in {"OWNER", "WRITE"} and not access.translator:
        fail("forbidden", "커밋 권한이 필요합니다.", 403)
    wc = workspace(db, problem, user)
    if values.expected_revision is not None and values.expected_revision != problem.revision:
        fail("preview_changed", "공유 리비전이 변경되었습니다. 변경사항을 다시 확인한 뒤 커밋하세요.", 409)
    if wc.version != values.version:
        fail("workspace_changed", "최신 작업 사본을 불러오세요.", 409)
    if access.role == "READ" and any(wc.document.get(k) != wc.base_document.get(k) for k in set(wc.document)|set(wc.base_document) if k not in {"statements", "translation_reviews"}):
        fail("translator_scope", "번역자는 지문 변경만 커밋할 수 있습니다.", 403)
    doc, conflicts = three_way_merge(wc.base_document, wc.document, problem.document)
    if conflicts:
        fail("revision_conflict", "동료의 변경과 충돌했습니다. 충돌 항목을 확인해 주세요.", 409, conflicts=conflicts)
    doc = validate_document(db, problem.id, doc)
    basis=problem.document.get("translation_source")
    if access.role == "READ" and basis and doc['statements'].get(basis)!=problem.document['statements'].get(basis):
        fail("translator_scope", "번역자는 기준 원문을 변경할 수 없습니다.", 403)
    if doc == problem.document:
        save_working(db, wc, doc, values.version)
        wc.base_revision = problem.revision
        wc.base_document = copy.deepcopy(doc)
        db.commit()
        return {"committed": False, "revision": problem.revision, "message": "변경사항이 없습니다."}
    number = problem.revision + 1
    updated = db.execute(update(Problem).where(Problem.id == problem_id, Problem.revision == problem.revision).values(document=doc, revision=number, updated_at=now()))
    changed_wc = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user.id, WorkingCopy.version == values.version).values(document=doc, base_document=doc, base_revision=number, version=values.version + 1, updated_at=now()))
    if not updated.rowcount or not changed_wc.rowcount:
        db.rollback()
        fail("revision_changed", "새 변경이 커밋되었습니다. 업데이트 후 다시 시도해 주세요.", 409)
    db.add(Revision(problem_id=problem_id, number=number, author_id=user.id, message=values.message, minor=values.minor, document=copy.deepcopy(doc)))
    db.commit()
    return {"committed": True, "revision": number, "version": values.version + 1}


@router.post("/{problem_id}/update")
def update_working(problem_id: str, values: WorkingAction, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user)
    wc = workspace(db, problem, user)
    doc, conflicts = three_way_merge(wc.base_document, wc.document, problem.document)
    if conflicts:
        fail("revision_conflict", "같은 항목을 수정한 변경이 있습니다.", 409, conflicts=conflicts)
    result = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user.id, WorkingCopy.version == values.version).values(document=doc, base_document=copy.deepcopy(problem.document), base_revision=problem.revision, version=values.version + 1, updated_at=now()))
    if not result.rowcount:
        db.rollback()
        fail("workspace_changed", "최신 작업 사본을 불러오세요.", 409)
    db.commit()
    return {"version": values.version + 1}


@router.post("/{problem_id}/discard")
def discard(problem_id: str, values: WorkingAction, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user)
    wc = workspace(db, problem, user)
    result = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user.id, WorkingCopy.version == values.version).values(document=copy.deepcopy(problem.document), base_document=copy.deepcopy(problem.document), base_revision=problem.revision, version=values.version + 1, updated_at=now()))
    if not result.rowcount:
        db.rollback()
        fail("workspace_changed", "최신 작업 사본을 불러오세요.", 409)
    db.commit()
    return {"version": values.version + 1}


@router.post("/{problem_id}/blobs", status_code=201)
async def upload_blob(problem_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    _, access = authorize(db, problem_id, user)
    if access.role not in {"OWNER", "WRITE"} and not access.translator:
        fail("forbidden", "업로드 권한이 필요합니다.", 403)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 16 * 1024 * 1024:
            fail("too_large", "파일은 16MiB 이하로 올려 주세요.", 413)
    result = put_blob(db, problem_id, bytes(content))
    db.commit()
    return result


@router.get("/{problem_id}/blobs/{sha}")
def download_blob(problem_id: str, sha: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    if not db.get(Blob, (problem_id, sha)):
        fail("not_found", "파일을 찾을 수 없습니다.", 404)
    return Response(read_blob(sha), media_type="application/octet-stream", headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})


@router.get("/{problem_id}/revisions")
def revisions(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    rows = db.scalars(select(Revision).where(Revision.problem_id == problem_id).order_by(Revision.number.desc())).all()
    return [{"number": r.number, "message": r.message, "minor": r.minor, "author": db.get(User, r.author_id).display_name, "created_at": r.created_at, "hash": document_hash(r.document)} for r in rows]


@router.get("/{problem_id}/revisions/{number}")
def revision(problem_id: str, number: int, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    row = db.get(Revision, (problem_id, number))
    if not row:
        fail("not_found", "리비전을 찾을 수 없습니다.", 404)
    return row.document


class Note(Strict):
    note: str = Field(max_length=50)


@router.put("/{problem_id}/note")
def note(problem_id: str, values: Note, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, write=True)
    problem.note = values.note
    db.commit()
    return {"note": problem.note}


class Deleted(Strict):
    deleted: bool


@router.patch("/{problem_id}/deleted")
def delete_problem(problem_id: str, values: Deleted, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, owner=True, allow_deleted=True)
    problem.deleted = values.deleted
    problem.updated_at = now()
    db.commit()
    return {"deleted": problem.deleted}


class Resolve(WorkingAction):
    revision: int = Field(ge=0)
    choices: dict[str, str]


@router.post("/{problem_id}/resolve")
def resolve_conflicts(problem_id: str, values: Resolve, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    if access.role not in {"WRITE", "OWNER"} and not access.translator:
        fail("forbidden", "충돌을 해결할 권한이 없습니다.", 403)
    wc = workspace(db, problem, user)
    if access.role == "READ" and any(wc.document[k] != wc.base_document[k] for k in wc.document if k != "statements"):
        fail("translator_scope", "번역자는 지문 변경만 커밋할 수 있습니다.", 403)
    if problem.revision != values.revision:
        fail("revision_changed", "공유 리비전이 변경되었습니다. 최신 충돌을 다시 확인하세요.", 409)
    merged, conflicts = three_way_merge(wc.base_document, wc.document, problem.document)
    if set(values.choices) != set(conflicts) or any(v not in {"ours", "theirs"} for v in values.choices.values()):
        fail("invalid_resolution", "모든 충돌 항목에서 유지할 변경을 선택하세요.")
    for path, choice in values.choices.items():
        if choice == "ours":
            continue
        parts = path.split("/")
        source = problem.document
        missing = False
        for part in parts:
            if not isinstance(source, dict) or part not in source:
                missing = True
                break
            source = source[part]
        target = merged
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        if missing:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = copy.deepcopy(source)
    doc = validate_document(db, problem_id, merged)
    changed = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user.id, WorkingCopy.version == values.version).values(document=doc, base_document=copy.deepcopy(problem.document), base_revision=problem.revision, version=values.version + 1, updated_at=now()))
    if not changed.rowcount:
        db.rollback()
        fail("workspace_changed", "작업 사본이 변경되었습니다. 다시 확인하세요.", 409)
    db.commit()
    return {"version": values.version + 1, "resolved": conflicts}
