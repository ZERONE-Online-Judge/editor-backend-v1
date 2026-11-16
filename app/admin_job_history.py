"""Purpose-signed, read-only administration of durable Editor job records.

Only the ZOJ service-master proxy signs this protocol. No Editor browser
session grants access, and neither source snapshots nor artifact credentials
cross the bridge. The only write is atomic consumption of an expiring nonce.
"""
import base64
import binascii
import hashlib
import hmac
import math
import re
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Job, LibraryNonce, Problem, User, now, session_dependency
from .domain import fail
from .job_categories import JobCategory, category_condition, job_category
from .job_errors import readable_result
from .job_outcomes import summarize
from .pagination import page_rows

PURPOSE = "zoj-editor-admin-history-v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_CLOCK_SKEW = 90
EMAIL = TypeAdapter(EmailStr)
KINDS = Literal["all", "prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"]
STATUSES = Literal["all", "queued", "running", "completed", "failed", "cancelled"]


def signed_history_request(request: Request, db: Session = Depends(session_dependency)):
    secret = get_settings().zoj_bridge_secret
    if not secret or len(secret.get_secret_value()) < 32:
        fail("admin_history_disabled", "Editor 작업 내역 연결이 설정되지 않았습니다.", 503)
    stamp = request.headers.get("x-editor-timestamp", "")
    nonce = request.headers.get("x-editor-nonce", "")
    encoded = request.headers.get("x-editor-identity", "")
    digest = request.headers.get("x-editor-content-sha256", "")
    signature = request.headers.get("x-editor-signature", "")
    at = now()
    invalid = (request.method != "GET" or not re.fullmatch(r"[0-9]{1,12}", stamp)
               or not re.fullmatch(r"[a-f0-9]{32}", nonce)
               or not re.fullmatch(r"[A-Za-z0-9_-]{2,512}", encoded)
               or digest != EMPTY_SHA256 or not re.fullmatch(r"[a-f0-9]{64}", signature)
               or request.headers.get("content-length", "0") != "0"
               or "transfer-encoding" in request.headers)
    if invalid or abs(at - int(stamp)) > MAX_CLOCK_SKEW:
        fail("admin_history_signature_invalid", "작업 내역 요청을 인증할 수 없습니다.", 401)
    try:
        identity = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("utf-8")
        EMAIL.validate_python(identity)
        raw_path = request.scope.get("raw_path", request.url.path.encode("ascii")).decode("ascii")
        query = request.scope.get("query_string", b"").decode("ascii")
    except (ValueError, UnicodeError, binascii.Error, ValidationError):
        fail("admin_history_signature_invalid", "작업 내역 요청을 인증할 수 없습니다.", 401)
    if len(query) > 4096:
        fail("invalid_request", "입력 값을 확인해 주세요.", 422)
    target = raw_path + ("?" + query if query else "")
    canonical = "\n".join([PURPOSE, request.method, target, stamp, nonce, encoded, digest])
    expected = hmac.new(secret.get_secret_value().encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        fail("admin_history_signature_invalid", "작업 내역 요청을 인증할 수 없습니다.", 401)
    # A unique constraint protects concurrent API workers as well as reloads.
    # Keep a future-dated request's nonce beyond its whole remaining window.
    db.execute(delete(LibraryNonce).where(LibraryNonce.expires_at < at))
    db.add(LibraryNonce(nonce=nonce, expires_at=at + MAX_CLOCK_SKEW * 2 + 1))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail("admin_history_replayed", "이미 처리한 작업 내역 요청입니다.", 401)
    return identity


router = APIRouter(prefix="/api/internal/admin/jobs", tags=["internal administration"],
                   dependencies=[Depends(signed_history_request)])


def safe_text(value, maximum=2000):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value[:maximum])
    value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [숨김]", value)
    value = re.sub(r"(?i)\b(token|secret|password|authorization|lease|signature|cookie)\s*[:=]\s*\S+", r"\1=[숨김]", value)
    value = re.sub(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+", "[주소 숨김]", value)
    value = re.sub(r"(?<![\w])(?:[A-Za-z]:\\|/)[^\s<>\"']+", "[경로 숨김]", value)
    return value


NUMBER, TEXT, BOOLEAN = "number", "text", "boolean"
COUNTER = {"current": NUMBER, "total": NUMBER, "percent": NUMBER}
SCOPED_COUNTER = {**COUNTER, "preparation": COUNTER, "judging": COUNTER}
PROGRESS = {**SCOPED_COUNTER, "phase": TEXT, "completed_solutions": NUMBER, "total_solutions": NUMBER,
            "testsets": ("map", SCOPED_COUNTER)}
CASE = {"testset": TEXT, "index": NUMBER, "group": TEXT, "verdict": TEXT, "score": NUMBER,
        "cpu_ms": NUMBER, "wall_ms": NUMBER, "memory_kb": NUMBER, "iteration": NUMBER, "seed": NUMBER}
EXPECTATION = {"testset": TEXT, "group": TEXT, "expected": TEXT, "matched": BOOLEAN,
               "skipped": BOOLEAN, "observed": ("list", TEXT)}
SOLUTION = {"name": TEXT, "tag": TEXT, "role": TEXT, "state": TEXT, "matched": BOOLEAN,
            "complete": BOOLEAN, "expectations_complete": BOOLEAN, "skipped": BOOLEAN,
            "completed_tests": NUMBER, "total_tests": NUMBER, "max_cpu_ms": NUMBER, "max_memory_kb": NUMBER,
            "observed_verdicts": ("list", TEXT), "passed_tests": NUMBER, "failed_tests": NUMBER, "unconfirmed_tests": NUMBER,
            "cases": ("list", CASE), "expectations": ("list", EXPECTATION)}
STRESS = {key: NUMBER for key in ("iterations_started", "iterations_completed", "elapsed_seconds", "duplicates_skipped",
                                "counterexample_count", "last_seed", "last_pattern_index", "evidence_bytes")}
STRESS.update(stop_reason=TEXT, phase=TEXT)
RESULT = {"summary": TEXT, "progress": NUMBER, "verified": BOOLEAN, "verification_skipped": BOOLEAN,
          "prepare_mode": TEXT, "preparation_total": NUMBER, "processed_testsets": ("list", TEXT),
          "expectations_version": NUMBER, "mismatches": ("list", TEXT), "error_code": TEXT,
          "verification_progress": PROGRESS, "solutions": ("list", SOLUTION), "stress_results": ("list", SOLUTION),
          "unit_tests": ("list", {"id": TEXT, "expected": TEXT, "actual": TEXT, "matched": BOOLEAN}),
          "unit_test_totals": ("map", NUMBER),
          "testsets": ("map", ("list", {"index": NUMBER, "group": TEXT, "validator": TEXT, "points": NUMBER, "sample": BOOLEAN})),
          "stress": STRESS, "iterations": NUMBER,
          "stress_result_summary": {"executions": NUMBER, "testcase_count": NUMBER, "max_cpu_ms": NUMBER, "max_memory_kb": NUMBER},
          "candidate_summaries": ("list", {"name": TEXT, "tag": TEXT, "tested": NUMBER, "counterexamples": NUMBER,
                                             "verdicts": ("map", NUMBER)}),
          "runtime": {"backend": TEXT, "runtime_revision": TEXT},
          "statements": ("map", {"html": TEXT, "pdf": TEXT}),
          "events": ("list", {"label": TEXT, "seed": NUMBER, "index": NUMBER, "testset": TEXT}),
          "auto_apply": {"status": TEXT, "code": TEXT},
          "assessment_failure": {"kind": TEXT, "code": TEXT}}
OPTIONS = {key: TEXT for key in ("name", "execution_backend", "snapshot", "testset", "prepare_mode", "package_type",
                                "export_format", "statement_language", "reference", "candidate", "stop_mode")}
OPTIONS.update({key: BOOLEAN for key in ("verify", "all_testsets", "single_verification", "check_only", "force_prepare",
                                        "auto_apply", "minimize", "stop_on_first", "focus_uncovered")})
OPTIONS.update({key: NUMBER for key in ("iterations", "duration_seconds", "seed_start", "seed_step", "counterexample_limit")})
OPTIONS.update(testsets=("list", TEXT), solutions=("list", TEXT), candidates=("list", TEXT), test_indices=("list", NUMBER))
OUTCOME = {"execution": TEXT, "assessment": {"kind": TEXT, "status": TEXT, "code": TEXT, "partial": BOOLEAN,
           "counts": {"total": NUMBER, "passed": NUMBER, "failed": NUMBER, "unconfirmed": NUMBER},
           "iterations": NUMBER, "found": NUMBER, "uncovered": NUMBER}}
OMIT = object()


class Projection:
    """An allowlisted, bounded copy; raw diagnostic/source fields stay local."""
    def __init__(self):
        self.remaining = 16000
        self.characters = 260000
        self.truncated = False

    def select(self, schema, value, depth=0):
        if self.remaining <= 0 or self.characters <= 0 or depth > 12:
            self.truncated = True
            return OMIT
        self.remaining -= 1
        if value is None:
            return None
        if schema == TEXT:
            if not isinstance(value, str):
                return OMIT
            maximum = min(2000, self.characters)
            self.truncated |= len(value) > maximum
            result = safe_text(value, maximum)
            self.characters -= len(result)
            return result
        if schema == NUMBER:
            return value if type(value) in {int, float} and math.isfinite(value) else OMIT
        if schema == BOOLEAN:
            return value if type(value) is bool else OMIT
        if isinstance(schema, dict) and isinstance(value, dict):
            result = {}
            for key, child in schema.items():
                if key in value:
                    item = self.select(child, value[key], depth + 1)
                    if item is not OMIT:
                        result[key] = item
            return result
        if isinstance(schema, tuple):
            kind, child = schema
            if kind == "list" and isinstance(value, list):
                self.truncated |= len(value) > 500
                result = []
                for item in value[:500]:
                    item = self.select(child, item, depth + 1)
                    if item is not OMIT:
                        result.append(item)
                return result
            if kind == "map" and isinstance(value, dict):
                self.truncated |= len(value) > 50
                result = {}
                for index, (key, item) in enumerate(value.items()):
                    if index == 50:
                        break
                    item = self.select(child, item, depth + 1)
                    if item is not OMIT:
                        result[safe_text(key, 100)] = item
                return result
        return OMIT


def outcome(job, result, options):
    view = SimpleNamespace(kind=job.kind, status=job.status, result=result, options=options)
    try:
        return Projection().select(OUTCOME, summarize(view, result))
    except (AttributeError, KeyError, TypeError, ValueError):
        # Legacy incomplete records remain visible without asserting success.
        return {"execution": job.status, "assessment": {"kind": "none", "status": "unknown", "code": "missing_evidence", "partial": True}}


def list_outcome(job, result, options):
    stress = result.get("stress")
    stress_counted = (job.kind == "stress" and isinstance(stress, dict)
                      and type(stress.get("counterexample_count")) is int and stress["counterexample_count"] >= 0)
    if (job.status in {"queued", "running"} or result.get("assessment_failure")
            or stress_counted or (job.kind == "package" and (
                options.get("verify") is False or result.get("verification_skipped")))):
        return outcome(job, result, options)
    # Listing cannot establish a legacy verification's result from verified
    # alone. Detailed assessment reads one job, never 20 large reports/poll.
    return {"execution": job.status, "assessment": {"kind": "none", "status": "unknown", "code": "detail_required", "partial": False}}


def solution_statistics(result):
    """Keep whole-run measurements accurate when the detailed cases are capped."""
    value = dict(result)
    for field in ("solutions", "stress_results"):
        if not isinstance(result.get(field), list):
            continue
        rows = []
        for row in result[field][:500]:
            if not isinstance(row, dict):
                continue
            cases = row.get("cases") if isinstance(row.get("cases"), list) else []
            cases = [case for case in cases if isinstance(case, dict)]
            confirmed = [case for case in cases if type(case.get("index")) is int and case["index"] > 0]
            passed = sum(case.get("verdict") == "AC" for case in confirmed)
            failed = sum(case.get("verdict") in {"WA", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT", "PARTIAL"} for case in confirmed)
            total = row.get("total_tests")
            total = total if type(total) is int and total >= 0 else len(confirmed)
            stats = {"observed_verdicts": sorted({case["verdict"] for case in cases if isinstance(case.get("verdict"), str)}),
                     "passed_tests": passed, "failed_tests": failed,
                     "unconfirmed_tests": max(0, total - passed - failed)}
            for source, target in (("cpu_ms", "max_cpu_ms"), ("memory_kb", "max_memory_kb")):
                observed = [case[source] for case in cases if type(case.get(source)) in {int, float} and math.isfinite(case[source])]
                if observed:
                    stats[target] = max(observed)
            rows.append({**row, **stats})
        value[field] = rows
    truncated = any(isinstance(result.get(field), list) and len(result[field]) > 500 for field in ("solutions", "stress_results"))
    return value, truncated


def job_json(row, *, detail=False):
    job = SimpleNamespace(**row)
    if detail:
        options = row["options"] if isinstance(row["options"], dict) else {}
        result = readable_result(row["result"]) if isinstance(row["result"], dict) else {}
    else:
        options = {key: row["option_" + key] for key in LIST_OPTIONS if row["option_" + key] is not None}
        result = {key: row["result_" + key] for key in LIST_RESULT if row["result_" + key] is not None}
    progress = result.get("progress", 0)
    progress = max(0, min(100, progress)) if type(progress) in {int, float} and math.isfinite(progress) else 0
    value = {"id": job.id, "name": safe_text(options.get("name", ""), 120),
             "problem_id": job.problem_id, "problem": {"id": job.problem_id, "slug": job.slug, "name": job.problem_name or job.slug, "deleted": job.deleted},
             "requester": {"id": job.user_id, "display_name": job.requester_name, "handle": job.requester_handle, "email": job.requester_email},
             "requested_by": job.user_id, "kind": job.kind,
             "category": job_category(SimpleNamespace(kind=job.kind, options=options, result=result)), "status": job.status,
             "revision": job.revision, "workspace_version": job.workspace_version,
             "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at,
             "outcome": outcome(job, result, options) if detail else list_outcome(job, result, options),
             "result": {"summary": safe_text(result.get("summary", ""), 1000), "progress": progress}}
    if detail:
        projection = Projection()
        prepared, statistics_truncated = solution_statistics(result)
        selected = projection.select(RESULT, prepared)
        value["result"] = {**selected, **value["result"]}
        if result.get("error"):
            value["result"]["has_error"] = True
        option_projection = Projection()
        value["options"] = option_projection.select(OPTIONS, options)
        value.update(result_redacted=True, result_truncated=projection.truncated or option_projection.truncated or statistics_truncated)
    return value


LIST_OPTIONS = ("name", "single_verification", "prepare_mode", "verify")
LIST_RESULT = ("summary", "progress", "prepare_mode", "assessment_failure", "stress", "iterations", "verification_skipped", "uncovered_candidates")


def query_jobs(*, detail=False):
    # Snapshot documents are intentionally excluded. Only public problem
    # metadata and the requesting actor accompany each page of job evidence.
    fields = [Job.id, Job.problem_id, Job.user_id, Job.kind, Job.status, Job.revision, Job.workspace_version,
              Job.created_at, Job.started_at, Job.finished_at, Problem.slug,
              Problem.document["info"]["name"].as_string().label("problem_name"), Problem.deleted,
              User.display_name.label("requester_name"), User.handle.label("requester_handle"), User.email.label("requester_email")]
    if detail:
        fields.extend([Job.options, Job.result])
    else:
        # JSON leaf extraction yields integers for SQLite booleans unless its
        # scalar type is explicit. Keep strict True/False outcome checks equal
        # to the full JSON detail response, including missing/null as None.
        fields.extend((Job.options[key].as_boolean() if key in {"single_verification", "verify"}
                       else Job.options[key]).label("option_" + key) for key in LIST_OPTIONS)
        fields.extend((Job.result[key].as_boolean() if key == "verification_skipped"
                       else Job.result[key]).label("result_" + key) for key in LIST_RESULT)
    return select(*fields).join(Problem, Problem.id == Job.problem_id).join(User, User.id == Job.user_id)


@router.get("")
def list_jobs(page: int = Query(default=1, ge=1, le=1000000), page_size: int = Query(default=20, ge=1, le=100),
              q: str = Query(default="", max_length=200), kind: KINDS = "all", category: JobCategory = "all",
              status: STATUSES = "all", db: Session = Depends(session_dependency)):
    query = query_jobs()
    if kind != "all":
        query = query.where(Job.kind == kind)
    if category != "all":
        query = query.where(category_condition(category))
    if status != "all":
        query = query.where(Job.status == status)
    if q.strip():
        needle = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(*(field.ilike(needle, escape="\\") for field in (
            Job.id, Job.options["name"].as_string(), Problem.slug, Problem.document["info"]["name"].as_string(),
            User.display_name, User.handle, User.email))))
    filtered = query.subquery()
    counts = dict(db.execute(select(filtered.c.status, func.count()).group_by(filtered.c.status)).all())
    rows, metadata = page_rows(db, query.order_by(Job.created_at.desc(), Job.id.desc()), page, page_size)
    return {**metadata, "summary": {"total": metadata["total"], **{key: counts.get(key, 0) for key in (
        "queued", "running", "completed", "failed", "cancelled")}}, "items": [job_json(row._mapping) for row in rows]}


@router.get("/{job_id}")
def get_job(job_id: str = Path(pattern=r"^[a-f0-9]{32}$"), db: Session = Depends(session_dependency)):
    row = db.execute(query_jobs(detail=True).where(Job.id == job_id)).mappings().first()
    if row is None:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    return job_json(row, detail=True)
