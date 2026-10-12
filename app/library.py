"""ZOJ-initiated access to the caller's own Editor projects only."""
import base64
import hashlib
import hmac
import json
import re
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import EmailStr, Field, TypeAdapter
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import get_settings
from .db import Folder, Job, LibraryNonce, Placement, Problem, User, WorkingCopy, now, session_dependency
from .domain import document_hash, fail, workspace
from .jobs import JobOptions, JobRequest, _create_job
from .schema import Strict

router = APIRouter(prefix="/api/zoj-library", tags=["ZOJ library"])


async def caller(request: Request, db: Session = Depends(session_dependency)):
    settings = get_settings()
    key = settings.zoj_bridge_secret.get_secret_value() if settings.zoj_bridge_secret else ""
    if len(key) < 32:
        fail("library_not_configured", "Editor와 ZOJ의 연결이 아직 설정되지 않았습니다.", 503)
    stamp, nonce = request.headers.get("x-editor-timestamp", ""), request.headers.get("x-editor-nonce", "")
    encoded, sha = request.headers.get("x-editor-identity", ""), request.headers.get("x-editor-content-sha256", "")
    signature = request.headers.get("x-editor-signature", "")
    if not re.fullmatch(r"[0-9]{10}", stamp) or abs(int(stamp) - now()) > 90 or not re.fullmatch(r"[a-f0-9]{32}", nonce) or not re.fullmatch(r"[a-f0-9]{64}", sha) or not re.fullmatch(r"[a-f0-9]{64}", signature) or len(encoded) > 512:
        fail("library_auth_invalid", "ZOJ 연결 인증이 유효하지 않습니다.", 401)
    path = request.url.path + ("?" + request.url.query if request.url.query else "")
    message = "\n".join(["zoj-library-v1", request.method, path, stamp, nonce, encoded, sha])
    if not hmac.compare_digest(hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest(), signature):
        fail("library_auth_invalid", "ZOJ 연결 인증이 유효하지 않습니다.", 401)
    try:
        email = str(TypeAdapter(EmailStr).validate_python(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b'-_', validate=True).decode())).lower()
    except (ValueError, UnicodeError):
        fail("library_auth_invalid", "연결 계정이 유효하지 않습니다.", 401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            fail("library_request_too_large", "요청 크기가 너무 큽니다.", 413)
    if hashlib.sha256(body).hexdigest() != sha:
        fail("library_content_changed", "연결 요청 내용이 변경되었습니다.", 401)
    request.state.library_body = bytes(body)
    db.execute(delete(LibraryNonce).where(LibraryNonce.expires_at < now()))
    db.add(LibraryNonce(nonce=nonce, expires_at=now() + 180))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail("library_replayed", "이미 처리한 연결 요청입니다.", 401)
    return email, db.scalar(select(User).where(func.lower(User.email) == email))


def own_problem(db, user, problem_id):
    row = db.get(Problem, problem_id) if user else None
    if not row or row.owner_id != user.id or row.deleted:
        fail("library_problem_not_found", "내 계정의 문제를 찾을 수 없습니다.", 404)
    return row


def package_json(job):
    return {"id": job.id, "status": job.status, "revision": job.revision, "options": job.options,
        "summary": job.result.get("error", job.result.get("summary", ""))[:2000],
        "progress": job.result.get("progress", 0), "package": job.result.get("package"), "created_at": job.created_at}


@router.get("/problems")
def list_projects(q: str = Query(default="", max_length=200), offset: int = Query(default=0, ge=0, le=100000),
        limit: int = Query(default=100, ge=1, le=200), folder: str = Query(default="all", pattern=r"^(all|root|[a-f0-9]{32})$"),
        identity=Depends(caller), db: Session = Depends(session_dependency)):
    email, user = identity
    result = {"email": email, "account_exists": bool(user), "editor_url": get_settings().public_origin,
              "problems": [], "folders": [], "total": 0, "offset": offset, "limit": limit}
    if not user:
        return result
    query = select(Problem).where(Problem.owner_id == user.id, Problem.deleted.is_(False))
    if folder != "all":
        if folder == "root":
            classified = select(Placement.problem_id).where(Placement.user_id == user.id, Placement.folder_id.is_not(None))
            query = query.where(Problem.id.not_in(classified))
        else:
            own_folder = db.get(Folder, folder)
            if not own_folder or own_folder.user_id != user.id:
                fail("library_folder_not_found", "내 폴더를 찾을 수 없습니다.", 404)
            placed = select(Placement.problem_id).where(Placement.user_id == user.id, Placement.folder_id == folder)
            query = query.where(Problem.id.in_(placed))
    if q.strip():
        needle = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(Problem.slug.ilike(needle, escape="\\"), Problem.document["info"]["name"].as_string().ilike(needle, escape="\\")))
    result["total"] = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Problem.updated_at.desc(), Problem.id.desc()).offset(offset).limit(limit)).all()
    ids = [p.id for p in rows]
    copies = {w.problem_id: w for w in db.scalars(select(WorkingCopy).where(WorkingCopy.user_id == user.id, WorkingCopy.problem_id.in_(ids)))}
    placements = {p.problem_id: p.folder_id for p in db.scalars(select(Placement).where(Placement.user_id == user.id, Placement.problem_id.in_(ids)))}
    for problem in rows:
        wc = copies.get(problem.id)
        result["problems"].append({"id": problem.id, "name": problem.document["info"]["name"], "slug": problem.slug,
            "revision": problem.revision, "updated_at": problem.updated_at, "folder_id": placements.get(problem.id),
            "uncommitted": bool(wc and wc.document != wc.base_document), "testsets": list(problem.document["testsets"]),
            "statement_languages": list(problem.document["statements"]),
            "url": get_settings().public_origin.rstrip("/") + "/problems/" + problem.id + "/packages"})
    result["folders"] = [{"id": f.id, "name": f.name, "parent_id": f.parent_id} for f in db.scalars(select(Folder).where(Folder.user_id == user.id).order_by(Folder.name_key))]
    return result


class PackageOptions(Strict):
    testset: str = Field(default="tests", min_length=1, max_length=100)
    statement_language: str = Field(default="korean", min_length=1, max_length=100)


@router.post("/problems/{problem_id}/packages", status_code=202)
def prepare_package(problem_id: str, request: Request, identity=Depends(caller), db: Session = Depends(session_dependency)):
    _, user = identity
    problem = own_problem(db, user, problem_id)
    try:
        values = PackageOptions.model_validate(json.loads(request.state.library_body))
    except ValueError:
        fail("invalid_request", "테스트 세트와 지문 언어를 확인하세요.", 422)
    wc = workspace(db, problem, user)
    result = _create_job(problem_id, JobRequest(kind="package", version=wc.version,
        options=JobOptions(export_format="zoj", verify=True, **values.model_dump()), reuse=True), user, db, from_committed_revision=True)
    return package_json(db.get(Job, result["id"]))


def own_package(db, user, problem_id, job_id):
    problem = own_problem(db, user, problem_id)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id or job.kind != "package" or job.options.get("export_format") != "zoj":
        fail("library_package_not_found", "검증 패키지를 찾을 수 없습니다.", 404)
    if job.revision != problem.revision or job.document_hash != document_hash(problem.document):
        fail("library_package_stale", "문제가 변경되었습니다. 커밋 후 패키지를 다시 준비하세요.", 409)
    return job


@router.get("/problems/{problem_id}/packages/{job_id}")
def package_status(problem_id: str, job_id: str, identity=Depends(caller), db: Session = Depends(session_dependency)):
    return package_json(own_package(db, identity[1], problem_id, job_id))


@router.get("/problems/{problem_id}/packages/{job_id}/download")
def package_download(problem_id: str, job_id: str, identity=Depends(caller), db: Session = Depends(session_dependency)):
    job = own_package(db, identity[1], problem_id, job_id)
    package = job.result.get("package", {})
    if job.status != "completed" or not package.get("verified") or package.get("format") != "zoj":
        fail("library_package_not_ready", "검증 패키지가 아직 준비되지 않았습니다.", 409)
    path = get_settings().data_dir / "packages" / (job.id + ".zip")
    if not path.is_file() or path.stat().st_size > get_settings().max_archive_bytes:
        fail("library_package_missing", "패키지 파일을 다시 생성하세요.", 409)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1048576):
            digest.update(chunk)
    if digest.hexdigest() != package["sha256"]:
        fail("library_package_changed", "패키지 무결성 검사에 실패했습니다.", 409)
    return FileResponse(path, media_type="application/zip", filename=package["filename"], headers={"X-Content-Sha256": package["sha256"]})
