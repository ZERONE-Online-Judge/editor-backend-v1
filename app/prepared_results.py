"""Apply successful generation atomically, without overwriting newer edits."""
import copy
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import load_only
from .db import Job, JobApplication, JobDiscard, WorkingCopy, now
from .domain import authorize, document_hash, fail, validate_document, workspace


def is_check_only(job):
    if job.kind != "prepare":
        return False
    mode = job.options.get("prepare_mode") or job.result.get("prepare_mode") or "full"
    # Older whole-input checks had no explicit marker; selected input
    # regeneration is still a generation operation with applicable files.
    return mode == "validate" or (mode == "inputs" and job.options.get("check_only", not job.options.get("test_indices")))


def check_only_condition():
    mode = func.coalesce(Job.options["prepare_mode"].as_string(), Job.result["prepare_mode"].as_string(), "full")
    legacy_whole_check = func.coalesce(Job.options["test_indices"].as_string(), "[]") == "[]"
    marked = func.coalesce(Job.options["check_only"].as_boolean(), legacy_whole_check)
    return and_(Job.kind == "prepare", or_(mode == "validate", and_(mode == "inputs", marked)))


def application_json(row):
    return {"applied_at": row.applied_at, "workspace_version": row.workspace_version,
            "document_hash": row.document_hash} if row else None


def pending_preparations(db, problem_id, user_id, current_hash):
    rows = db.scalars(select(Job).options(load_only(Job.id, Job.document_hash, Job.created_at))
        .outerjoin(JobApplication, (JobApplication.job_id == Job.id) & (JobApplication.user_id == user_id))
        .outerjoin(JobDiscard, (JobDiscard.job_id == Job.id) & (JobDiscard.user_id == user_id))
        .where(Job.problem_id == problem_id, Job.user_id == user_id, Job.kind == "prepare",
               Job.status == "completed", JobApplication.job_id.is_(None), JobDiscard.job_id.is_(None),
               ~check_only_condition())
        .order_by(Job.created_at.desc())).all()
    return [{"id": row.id, "created_at": row.created_at,
             "stale": row.document_hash != current_hash} for row in rows]


def apply_prepared_results(db, job, user, version):
    problem, _ = authorize(db, job.problem_id, user, permission="edit_tests")
    wc = workspace(db, problem, user)
    db.refresh(wc, with_for_update=True)
    if job.kind != "prepare" or job.status != "completed":
        fail("not_ready", "완료된 입력·정답 생성 작업만 반영할 수 있습니다.")
    if is_check_only(job):
        fail("check_only", "입력 검사는 결과만 확인하는 작업입니다. 작업 사본에 반영할 생성 결과가 없습니다.")
    previous = db.get(JobApplication, (job.id, user.id))
    if previous:
        return {"version": wc.version, "application": application_json(previous)}
    if db.get(JobDiscard, (job.id, user.id)):
        fail("result_discarded", "버린 생성 결과는 반영할 수 없습니다. 입력·정답 생성을 다시 요청하세요.", 409)
    if wc.version != version:
        fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
    if job.document_hash != document_hash(wc.document):
        fail("stale_results", "실행 이후 자료가 변경되었습니다. 입력·정답을 다시 생성하세요.", 409)
    doc = copy.deepcopy(wc.document)
    for name, tests in job.result.get("testsets", {}).items():
        for test in tests:
            key = next((key for key, saved in doc["testsets"][name]["tests"].items()
                        if saved["index"] == test["index"]), str(test["index"]))
            doc["testsets"][name]["tests"][key] = {k: v for k, v in test.items()
                if k in {"index", "input_sha", "answer_sha", "command", "group", "points", "description", "sample", "statement_input", "statement_output", "verify_statement_io"}}
        doc["testsets"][name]["script"] = ""
    doc = validate_document(db, problem.id, doc)
    changed = document_hash(doc) != document_hash(wc.document)
    next_version = version + int(changed)
    # Even unchanged outputs claim the version to serialize application against
    # concurrent saves. The copy and its application record commit together.
    result = db.execute(update(WorkingCopy).where(
        WorkingCopy.problem_id == problem.id, WorkingCopy.user_id == user.id,
        WorkingCopy.version == version).values(document=doc, version=next_version, updated_at=now()))
    if not result.rowcount:
        db.rollback()
        fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
    record = JobApplication(job_id=job.id, user_id=user.id, applied_at=now(),
                            workspace_version=next_version, document_hash=document_hash(doc))
    db.add(record)
    db.commit()
    return {"version": next_version, "application": application_json(record)}


def discard_prepared_results(db, job, user):
    problem, _ = authorize(db, job.problem_id, user, permission="edit_tests")
    wc = workspace(db, problem, user)
    db.refresh(wc, with_for_update=True)
    if job.kind != "prepare" or job.status != "completed":
        fail("not_ready", "완료된 입력·정답 생성 결과만 버릴 수 있습니다.")
    if is_check_only(job):
        fail("check_only", "입력 검사는 반영하거나 버릴 생성 결과가 없습니다. 검사 기록은 실행 결과에서 확인하세요.")
    if db.get(JobApplication, (job.id, user.id)):
        fail("already_applied", "이미 사본에 반영한 결과입니다. 반영된 자료는 테스트에서 수정해 주세요.", 409)
    previous = db.get(JobDiscard, (job.id, user.id))
    if previous:
        return {"discarded_at": previous.discarded_at}
    row = JobDiscard(job_id=job.id, user_id=user.id)
    db.add(row)
    db.commit()
    return {"discarded_at": row.discarded_at}
