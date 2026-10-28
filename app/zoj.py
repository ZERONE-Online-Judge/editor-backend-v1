"""Publish an immutable ZIP using the authenticated writer's ZOJ identity."""
import base64
import hashlib
import hmac
import ssl
import time
import uuid
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends, Path
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import current_user
from .config import get_settings
from .db import Job, Publication, User, WorkingCopy, session_dependency
from .domain import authorize, document_hash, fail
from .schema import Strict
from .pagination import actor_json, page_rows, page_number, page_length

router = APIRouter(prefix="/api", tags=["ZOJ publishing"])


def signed_headers(method, path, email, body_sha):
    settings = get_settings()
    if not settings.zoj_api_url or not settings.zoj_bridge_secret:
        fail("zoj_not_configured", "ZOJ 전송 연결이 아직 설정되지 않았습니다.", 503)
    encoded = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")
    timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
    canonical = "\n".join(["zoj-editor-v1", method, path, timestamp, nonce, encoded, body_sha])
    signature = hmac.new(settings.zoj_bridge_secret.get_secret_value().encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"X-Editor-Identity": encoded, "X-Editor-Timestamp": timestamp, "X-Editor-Nonce": nonce,
        "X-Editor-Content-Sha256": body_sha, "X-Editor-Signature": signature, "Content-Type": "application/zip"}


def remote(method, path, user, package=None, sha=None, *, timeout=120):
    settings = get_settings()
    headers = signed_headers(method, path, user.email, sha or hashlib.sha256(b"").hexdigest())
    target = settings.zoj_api_url.rstrip("/") + path.removeprefix("/api")
    context = ssl.create_default_context(cafile=str(settings.zoj_ca_file)) if settings.zoj_ca_file else True
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5), verify=context, follow_redirects=False, trust_env=False) as client:
            if package:
                with package.open("rb") as body:
                    response = client.request(method, target, headers=headers, content=body)
            else:
                response = client.request(method, target, headers=headers)
        if response.is_redirect:
            fail("zoj_transport_error", "ZOJ API 주소에서 리다이렉트가 발생했습니다. 서버 설정을 확인하세요.", 502)
        result = response.json()
        if response.is_error:
            error = result.get("error", {})
            fail(error.get("code", "zoj_rejected"), error.get("message", "ZOJ 전송을 처리할 수 없습니다."), response.status_code)
        return result["data"]
    except (httpx.HTTPError, ValueError, KeyError):
        fail("zoj_unavailable", "ZOJ 서버에 연결하지 못했습니다. 다시 전송해도 같은 요청은 중복 등록되지 않습니다.", 502)


@router.get("/zoj/contests")
def contests(user: User = Depends(current_user)):
    settings = get_settings()
    if not settings.zoj_api_url:
        return {"configured": False, "email": user.email, "contests": []}
    result = remote("GET", "/api/editor/contests", user)
    if result["email"].lower() != user.email.lower():
        fail("zoj_identity_mismatch", "ZOJ 응답의 계정 정보가 다릅니다.", 502)
    return {"configured": True, **result}


@router.get("/zoj/contests/{contest_id}/problems")
def contest_problems(contest_id: str = Path(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"), user: User = Depends(current_user)):
    # The destination service rechecks the caller's current contest role.
    return remote("GET", f"/api/editor/contests/{contest_id}/problems", user)


class Publish(Strict):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    contest_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    division_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    problem_code: str = Field(min_length=1, max_length=16)

    @field_validator("problem_code")
    @classmethod
    def clean_code(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Use a nonempty problem code")
        return value


def publication_json(row, requester=None):
    return {"operation_id": row.operation_id, "job_id": row.job_id, "contest_id": row.contest_id,
        "division_id": row.division_id, "problem_code": row.problem_code, "created_at": row.created_at, "result": row.result,
        "requester": actor_json(requester)}


@router.get("/problems/{problem_id}/zoj-publications")
def publications(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency),
                 page: int | None = page_number(), page_size: int = page_length()):
    authorize(db, problem_id, user)
    query = select(Publication, User).join(User, User.id == Publication.user_id).where(Publication.problem_id == problem_id, Publication.result["problem_id"].as_string().is_not(None)).order_by(Publication.created_at.desc(), Publication.operation_id.desc())
    rows, metadata = page_rows(db, query, page, page_size) if page else (db.execute(query.limit(100)).all(), None)
    items = [publication_json(row, actor) for row, actor in rows]
    return {**metadata, "items": items} if metadata else items


def require_transfer_copy(problem, user, db):
    wc = db.get(WorkingCopy, (problem.id, user.id))
    if wc and document_hash(wc.document) != document_hash(problem.document):
        if wc.document == wc.base_document:
            fail("workspace_update_required", "새 공유 리비전이 있습니다. 문제 작업 공간에서 업데이트한 뒤 다시 준비하세요.", 409)
        fail("commit_required", "변경사항을 먼저 커밋한 뒤 검증 패키지를 준비하세요.", 409)


def verified_package(problem_id, job_id, user, db):
    problem, _ = authorize(db, problem_id, user, permission="publish")
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id or job.kind != "package" or job.status != "completed" or job.result.get("package", {}).get("format") != "zoj" or not job.result.get("package", {}).get("verified"):
        fail("zoj_package_required", "검증을 완료한 ZOJ 형식 패키지를 먼저 만드세요.", 409)
    if job.document_hash != document_hash(problem.document) or job.revision != problem.revision:
        fail("stale_package", "문제가 변경되었습니다. 커밋 후 ZOJ 패키지를 다시 만드세요.", 409)
    require_transfer_copy(problem, user, db)
    path = get_settings().data_dir / "packages" / (job.id + ".zip")
    if not path.is_file() or path.stat().st_size > get_settings().max_archive_bytes:
        fail("zoj_package_missing", "보관된 패키지를 찾을 수 없습니다. 다시 생성하세요.", 409)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1048576):
            digest.update(chunk)
    if digest.hexdigest() != job.result["package"]["sha256"]:
        fail("zoj_package_changed", "보관된 패키지의 무결성 검사가 실패했습니다.", 409)
    return job, path, digest.hexdigest()


@router.post("/problems/{problem_id}/publish-zoj")
def publish(problem_id: str, values: Publish, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    job, path, sha = verified_package(problem_id, values.job_id, user, db)
    operation = hashlib.sha256("\n".join([user.id, job.id, values.contest_id, values.division_id, values.problem_code]).encode()).hexdigest()[:32]
    row = db.get(Publication, operation)
    if not row:
        row = Publication(operation_id=operation, user_id=user.id, problem_id=problem_id, job_id=job.id,
            contest_id=values.contest_id, division_id=values.division_id, problem_code=values.problem_code)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(Publication, operation)
    query = urlencode({"division_id": values.division_id, "problem_code": values.problem_code, "operation_id": operation})
    # ZOJ rechecks current roles and contest lock inside the import transaction.
    result = remote("POST", f"/api/editor/contests/{values.contest_id}/imports?{query}", user, path, sha)
    row.result = {"problem_id": result["problem_id"], "title": result["title"], "revision": job.revision,
        "url": get_settings().zoj_public_origin.rstrip("/") + f"/operator/contests/{values.contest_id}/problems"}
    db.commit()
    return publication_json(row, db.get(User, row.user_id))


class UpdatePreview(Strict):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    contest_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    target_problem_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


class PackageUpdate(UpdatePreview):
    target_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1000)
    confirm_running: bool = False

    @field_validator("reason")
    @classmethod
    def reason_required(cls, value):
        if not value.strip():
            raise ValueError("업데이트 이유를 입력하세요.")
        return value.strip()


@router.post("/problems/{problem_id}/zoj-updates:preview")
def preview_update(problem_id: str, values: UpdatePreview, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    _, path, sha = verified_package(problem_id, values.job_id, user, db)
    return remote("POST", f"/api/editor/contests/{values.contest_id}/problems/{values.target_problem_id}/package-updates:preview", user, path, sha)


@router.post("/problems/{problem_id}/zoj-updates")
def update_package(problem_id: str, values: PackageUpdate, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    job, path, sha = verified_package(problem_id, values.job_id, user, db)
    if sha != values.archive_sha256:
        fail("package_update_archive_changed", "패키지가 변경되었습니다. 변경 내용을 다시 확인하세요.", 409)
    query = urlencode({"target_hash": values.target_hash, "reason": values.reason,
                       "confirm_running": str(values.confirm_running).lower(),
                       "editor_problem_id": problem_id, "editor_revision": job.revision})
    result = remote("POST", f"/api/editor/contests/{values.contest_id}/problems/{values.target_problem_id}/package-updates?{query}", user, path, sha)
    problem = result["problem"]
    operation = hashlib.sha256("\n".join([user.id, job.id, values.contest_id, values.target_problem_id, values.target_hash]).encode()).hexdigest()[:32]
    row = db.get(Publication, operation)
    if not row:
        row = Publication(operation_id=operation, user_id=user.id, problem_id=problem_id, job_id=job.id,
            contest_id=values.contest_id, division_id=problem["division_id"], problem_code=problem["problem_code"])
        db.add(row)
    origin = get_settings().zoj_public_origin.rstrip("/")
    row.result = {"problem_id": problem["problem_id"], "title": problem["title"], "revision": job.revision,
        "url": origin + f"/operator/contests/{values.contest_id}/problems", "kind": "update"}
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {**result, "backup_url": origin + result["backup_url"], "revision": job.revision}
