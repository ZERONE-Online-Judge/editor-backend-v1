"""Publish a verified immutable ZIP using the server's verified account identity."""
import base64
import hashlib
import hmac
import ssl
import time
import uuid
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import current_user
from .config import get_settings
from .db import Job, Publication, User, WorkingCopy, session_dependency
from .domain import authorize, document_hash, fail
from .schema import Strict

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


def publication_json(row):
    return {"operation_id": row.operation_id, "job_id": row.job_id, "contest_id": row.contest_id,
        "division_id": row.division_id, "problem_code": row.problem_code, "created_at": row.created_at, "result": row.result}


@router.get("/problems/{problem_id}/zoj-publications")
def publications(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    return [publication_json(row) for row in db.scalars(select(Publication).where(Publication.problem_id == problem_id).order_by(Publication.created_at.desc()).limit(100)) if row.result]


@router.post("/problems/{problem_id}/publish-zoj")
def publish(problem_id: str, values: Publish, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, owner=True)
    job = db.get(Job, values.job_id)
    if not job or job.problem_id != problem_id or job.kind != "package" or job.status != "completed" or job.result.get("package", {}).get("format") != "zoj" or not job.result["package"].get("verified"):
        fail("zoj_package_required", "검증을 통과한 ZOJ 형식 패키지를 먼저 만드세요.", 409)
    wc = db.get(WorkingCopy, (problem_id, user.id))
    if job.document_hash != document_hash(problem.document) or job.revision != problem.revision or (wc and document_hash(wc.document) != job.document_hash):
        fail("stale_package", "문제가 변경되었습니다. 커밋 후 ZOJ 패키지를 다시 만드세요.", 409)
    path = get_settings().data_dir / "packages" / (job.id + ".zip")
    if not path.is_file() or path.stat().st_size > get_settings().max_archive_bytes:
        fail("zoj_package_missing", "보관된 패키지를 찾을 수 없습니다. 다시 생성하세요.", 409)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1048576):
            digest.update(chunk)
    if digest.hexdigest() != job.result["package"]["sha256"]:
        fail("zoj_package_changed", "보관된 패키지의 무결성 검사가 실패했습니다.", 409)
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
    result = remote("POST", f"/api/editor/contests/{values.contest_id}/imports?{query}", user, path, digest.hexdigest())
    row.result = {"problem_id": result["problem_id"], "title": result["title"], "revision": job.revision,
        "url": get_settings().zoj_public_origin.rstrip("/") + f"/operator/contests/{values.contest_id}/problems"}
    db.commit()
    return publication_json(row)
