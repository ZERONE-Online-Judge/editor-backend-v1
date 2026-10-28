"""Editor-owned picker and package preparation for the single ZOJ transfer flow."""
from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from .auth import current_user
from .config import get_settings
from .db import Folder, Job, Placement, Problem, User, WorkingCopy, session_dependency
from .domain import document_hash, fail, workspace
from .jobs import JobOptions, JobRequest, _create_job, job_json
from .schema import Strict
from .zoj import require_transfer_copy
from .folders import accessible_folders
from .permissions import has
from .folder_access import effective_access, writable_problem_ids, workspace_role
from .db import SharedPlacement

router = APIRouter(prefix="/api/zoj-transfer", tags=["ZOJ transfer"])



def editable_problem(db, user, problem_id):
    row = db.get(Problem, problem_id) if user else None
    access = effective_access(db, problem_id, user.id) if row else None
    if not row or row.deleted or not access or not has(access, "publish"):
        fail("library_problem_not_found", "쓰기 권한이 있는 문제를 찾을 수 없습니다.", 404)
    return row


def package_json(job):
    return job_json(job)

@router.get("/problems")
def list_projects(q: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=100, ge=1, le=200), folder: str = Query(default="all", pattern=r"^(all|root|[a-f0-9]{32})$"),
        user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    email = user.email
    result = {"email": email, "account_exists": bool(user), "editor_url": get_settings().public_origin,
              "problems": [], "folders": [], "total": 0, "offset": offset, "limit": limit}
    if not user:
        return result
    available = accessible_folders(db, user)
    shared = select(SharedPlacement.problem_id).join(Folder, Folder.id == SharedPlacement.folder_id).where(Folder.id.in_([f.id for f in available if f.workspace_id]))
    query = select(Problem).where(Problem.id.in_(writable_problem_ids(user.id)), Problem.deleted.is_(False))
    if folder != "all":
        if folder == "root":
            classified = select(Placement.problem_id).where(Placement.user_id == user.id, Placement.folder_id.is_not(None))
            query = query.where(Problem.id.not_in(classified), Problem.id.not_in(shared))
        else:
            own_folder = db.get(Folder, folder)
            if not own_folder or (not workspace_role(db, own_folder.workspace_id, user.id) if own_folder.workspace_id else own_folder.user_id != user.id):
                fail("library_folder_not_found", "내 폴더를 찾을 수 없습니다.", 404)
            placed = select(SharedPlacement.problem_id).where(SharedPlacement.folder_id == folder) if own_folder.workspace_id else select(Placement.problem_id).where(Placement.user_id == user.id, Placement.folder_id == folder, Placement.problem_id.not_in(shared))
            query = query.where(Problem.id.in_(placed))
    if q.strip():
        needle = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(Problem.slug.ilike(needle, escape="\\"), Problem.document["info"]["name"].as_string().ilike(needle, escape="\\")))
    result["total"] = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Problem.updated_at.desc(), Problem.id.desc()).offset(offset).limit(limit)).all()
    ids = [p.id for p in rows]
    copies = {w.problem_id: w for w in db.scalars(select(WorkingCopy).where(WorkingCopy.user_id == user.id, WorkingCopy.problem_id.in_(ids)))}
    placements = {p.problem_id: p.folder_id for p in db.scalars(select(Placement).where(Placement.user_id == user.id, Placement.problem_id.in_(ids)))}
    placements.update({p.problem_id:p.folder_id for p in db.scalars(select(SharedPlacement).where(SharedPlacement.problem_id.in_(ids), SharedPlacement.folder_id.in_([f.id for f in available if f.workspace_id])))})
    for problem in rows:
        wc = copies.get(problem.id)
        result["problems"].append({"id": problem.id, "name": problem.document["info"]["name"], "slug": problem.slug,
            "revision": problem.revision, "updated_at": problem.updated_at, "folder_id": placements.get(problem.id),
            "uncommitted": bool(wc and wc.document != wc.base_document), "testsets": list(problem.document["testsets"]),
            "statement_languages": list(problem.document["statements"]),
            "url": get_settings().public_origin.rstrip("/") + "/problems/" + problem.id + "/packages"})
    result["folders"] = [{"id": f.id, "name": f.name, "parent_id": f.parent_id} for f in available]
    return result


class PackageOptions(Strict):
    testset: str = Field(default="tests", min_length=1, max_length=100)
    testsets: list[str] | None = Field(default=None, min_length=1, max_length=20)
    statement_language: str = Field(default="korean", min_length=1, max_length=100)


@router.post("/problems/{problem_id}/packages", status_code=202)
def prepare_package(problem_id: str, values: PackageOptions, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem = editable_problem(db, user, problem_id)
    require_transfer_copy(problem, user, db)
    wc = workspace(db, problem, user)
    result = _create_job(problem_id, JobRequest(kind="package", version=wc.version,
        options=JobOptions(export_format="zoj", verify=True, **values.model_dump()), reuse=True), user, db)
    return package_json(db.get(Job, result["id"]))


def own_package(db, user, problem_id, job_id):
    problem = editable_problem(db, user, problem_id)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id or job.kind != "package" or job.options.get("export_format") != "zoj":
        fail("library_package_not_found", "검증 패키지를 찾을 수 없습니다.", 404)
    if job.revision != problem.revision or job.document_hash != document_hash(problem.document):
        fail("library_package_stale", "문제가 변경되었습니다. 커밋 후 패키지를 다시 준비하세요.", 409)
    return job


@router.get("/problems/{problem_id}/packages/{job_id}")
def package_status(problem_id: str, job_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    return package_json(own_package(db, user, problem_id, job_id))
