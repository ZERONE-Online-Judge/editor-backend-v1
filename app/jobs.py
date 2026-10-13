import copy
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session, load_only
from .config import get_settings
from .auth import current_user
from .db import Job, JobApplication, User, now, session_dependency
from .domain import authorize, cautions, document_hash, fail, workspace
from .schema import Strict
from .prepared_results import application_json, apply_prepared_results, pending_preparations

router = APIRouter(prefix="/api/problems", tags=["execution"])


@router.get("/{problem_id}/preparation-status")
def preparation_status(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    wc = workspace(db, problem, user)
    editable = access.role in {"OWNER", "WRITE"}
    return {"version": wc.version, "pending_preparations":
            pending_preparations(db, problem_id, user.id, document_hash(wc.document)) if editable else []}


class JobOptions(Strict):
    auto_apply: bool = False
    execution_backend: Literal["editor", "zoj"] | None = None
    testset: str = "tests"
    solutions: list[str] = Field(default_factory=list, max_length=200)
    test_indices: list[int] = Field(default_factory=list, max_length=1000)
    generator: str = Field(default="", max_length=4096)
    reference: str = ""
    candidate: str = ""
    iterations: int = Field(default=50, ge=1, le=1000)
    verify: bool = True
    package_type: Literal["linux", "sources"] = "linux"
    export_format: Literal["editor", "zoj"] = "editor"
    statement_language: str = Field(default="korean", min_length=1, max_length=100)


class JobRequest(Strict):
    kind: Literal["prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"]
    version: int = Field(ge=0)
    options: JobOptions = Field(default_factory=JobOptions)
    reuse: bool = True


def job_json(job, include_result=True, application=None):
    return {"id": job.id, "problem_id": job.problem_id, "kind": job.kind, "status": job.status,
            "document_hash": job.document_hash, "workspace_version": job.workspace_version,
            "revision": job.revision, "options": job.options, "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "application": application_json(application),
            "result": job.result if include_result else {"summary": job.result.get("summary", ""), "progress": job.result.get("progress", 0)}}


@router.post("/{problem_id}/jobs", status_code=202)
def create_job(problem_id: str, values: JobRequest, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    return _create_job(problem_id, values, user, db)


def _create_job(problem_id: str, values: JobRequest, user: User, db: Session, *, from_committed_revision: bool = False):
    problem, _ = authorize(db, problem_id, user, write=values.kind != "render")
    wc = workspace(db, problem, user)
    if wc.version != values.version:
        fail("workspace_changed", "최신 작업 사본을 저장한 뒤 실행하세요.", 409)
    doc = copy.deepcopy(problem.document if values.kind == "package" else wc.document)
    if values.kind == "package" and wc.document != problem.document and not from_committed_revision:
        fail("commit_required", "패키지를 만들기 전에 변경사항을 커밋하세요.", 409)
    options = values.options.model_dump()
    if options["auto_apply"] and values.kind != "prepare":
        fail("invalid_options", "자동 반영은 입력·정답 생성에서만 사용할 수 있습니다.")
    if values.kind == "package" and options["export_format"] == "zoj":
        from .zoj_package import check_compatibility
        check_compatibility(doc, options)
    if values.kind in {"package", "verify"}:
        blockers = [c for c in cautions(doc) if c["blocking"]]
        if blockers:
            fail("not_ready", "필수 자료를 먼저 준비하세요.", cautions=blockers)
    options["runtime_revision"] = get_settings().runtime_revision
    if options["testset"] not in doc["testsets"]:
        fail("invalid_testset", "테스트 세트를 찾을 수 없습니다.")
    for name in [*options["solutions"], options["reference"], options["candidate"]]:
        if name and name not in doc["solutions"]:
            fail("invalid_solution", "풀이 파일을 찾을 수 없습니다.")
    options["execution_backend"] = options["execution_backend"] or get_settings().judge_backend
    if values.kind not in {"verify", "package"} or (values.kind == "package" and not options["verify"]):
        options["execution_backend"] = "editor"
    if options["execution_backend"] == "zoj":
        from .judge_bridge import compatible, capabilities
        compatible(doc, options, values.kind)
        options["judge_revision"] = capabilities(user)["runtime_revision"]
    hash_value = document_hash(doc)
    if values.reuse:
        cached = db.scalars(select(Job).where(Job.problem_id == problem_id, Job.document_hash == hash_value, Job.kind == values.kind, Job.status.in_(["queued", "running", "completed"])).order_by(Job.created_at.desc())).all()
        for row in cached:
            if row.options == options:
                return job_json(row, application=db.get(JobApplication, (row.id, user.id)))
    active = db.scalars(select(Job).where(Job.user_id == user.id, Job.status.in_(["queued", "running"]))).all()
    if len(active) >= 5:
        fail("too_many_jobs", "동시에 요청할 수 있는 검증 작업은 5개입니다.", 429)
    row = Job(problem_id=problem_id, user_id=user.id, kind=values.kind, document=doc, document_hash=hash_value,
              workspace_version=wc.version, revision=problem.revision, options=options)
    db.add(row)
    db.commit()
    return job_json(row)


@router.get("/{problem_id}/jobs")
def list_jobs(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    rows = db.execute(select(Job, JobApplication).outerjoin(JobApplication,
        (JobApplication.job_id == Job.id) & (JobApplication.user_id == user.id))
        .options(load_only(Job.id, Job.problem_id, Job.kind, Job.status, Job.document_hash, Job.workspace_version,
            Job.revision, Job.options, Job.created_at, Job.started_at, Job.finished_at, Job.result))
        .where(Job.problem_id == problem_id).order_by(Job.created_at.desc()).limit(100))
    return [job_json(row, False, applied) for row, applied in rows]


@router.get("/{problem_id}/jobs/{job_id}")
def get_job(problem_id: str, job_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    row = db.get(Job, job_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    return job_json(row, application=db.get(JobApplication, (row.id, user.id)))


@router.post("/{problem_id}/jobs/{job_id}/cancel")
def cancel_job(problem_id: str, job_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user, write=True)
    row = db.get(Job, job_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    db.execute(update(Job).where(Job.id == job_id, Job.status.in_(["queued", "running"])).values(status="cancelled", finished_at=now()))
    db.commit()
    db.refresh(row)
    return job_json(row)


class ApplyResults(Strict):
    version: int = Field(ge=0)


@router.post("/{problem_id}/jobs/{job_id}/apply")
def apply_prepared(problem_id: str, job_id: str, values: ApplyResults, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, write=True)
    row = db.get(Job, job_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    return apply_prepared_results(db, row, user, values.version)
