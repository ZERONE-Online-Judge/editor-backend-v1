"""Adopt one fully verified immutable temporary source into a personal copy."""
import copy
import hashlib
import re

from sqlalchemy import select, update

from .db import Job, JobApplication, JobDiscard, WorkingCopy, now
from .domain import authorize, document_hash, expanded_tests, fail, validate_document, workspace
from .expectations import VERSION, evaluate
from .invocation_scope import selected_testsets
from .prepared_results import application_json
from .schema import Solution
from .storage import read_blob


EXTENSIONS = {"c99": ".c", "cpp17": ".cpp", "python3": ".py", "pypy3": ".py", "java8": ".java", "kotlin": ".kt"}
TAGS = {"OK", "RJ", "TL", "TO", "TM", "WA", "ML", "RE"}
VERDICTS = {"AC", "WA", "PARTIAL", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT"}


def is_single(job):
    return job.kind == "verify" and job.options.get("single_verification") is True


def valid_source(source):
    if (not isinstance(source, dict) or not isinstance(source.get("language"), str) or source["language"] not in EXTENSIONS
            or not isinstance(source.get("tag"), str) or source["tag"] not in TAGS):
        return False
    name, extension = source.get("name", ""), EXTENSIONS[source["language"]]
    return isinstance(name, str) and len(name) <= 100 and name.endswith(extension) and bool(
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name[:-len(extension)]))


def validate_request(kind, options):
    sources = options["temporary_solutions"]
    if len(sources) == 1 and sources[0]["tag"] not in TAGS:
        fail("invalid_single_tag", "ZOJ 단일 검증은 MA·NR·PE 기대 태그를 지원하지 않습니다.", supported_tags=sorted(TAGS))
    if (kind != "verify" or len(sources) != 1 or not valid_source(sources[0])
            or options["solutions"] != [sources[0]["name"]] or not sources[0]["content"].strip()):
        fail("invalid_single_verification", "단일 검증에는 ZOJ 언어·기대 태그와 일치하는 파일명으로 임시 코드 하나만 지정하세요.")


def saved_source(job):
    sources = job.options.get("temporary_solutions", [])
    if not is_single(job) or len(sources) != 1 or not valid_source(sources[0]):
        fail("invalid_single_verification", "단일 검증의 저장된 코드를 확인할 수 없습니다.")
    metadata = sources[0]
    source = job.document.get("solutions", {}).get(metadata["name"])
    if (not isinstance(source, dict) or any(source.get(key) != value for key, value in metadata.items())
            or source.get("extra_tags") or source.get("group_tags")
            or job.options.get("solutions") != [metadata["name"]]):
        fail("invalid_single_verification", "단일 검증의 저장된 코드와 실행 범위가 일치하지 않습니다.")
    try:
        source = Solution.model_validate(source).model_dump()
    except ValueError:
        fail("invalid_single_verification", "저장된 코드 파일의 정보를 확인할 수 없습니다.")
    if source["kind"] != "source" or source["compile_with_solution"] or source["entry_class"] or source["size"] > 1048576:
        fail("invalid_single_verification", "저장된 단일 검증 코드의 파일 정보가 올바르지 않습니다.")
    return source


def complete_match(job, source):
    try:
        _complete_match(job, source)
    except (KeyError, TypeError, ValueError, AttributeError):
        fail("incomplete_verification", "저장된 단일 검증 결과가 불완전합니다. 다시 검증하세요.")


def _complete_match(job, source):
    """Require all requested testcase identities and complete expected evidence."""
    result = job.result
    rows = result.get("solutions", [])
    if (job.status != "completed" or result.get("verified") is not True or result.get("expectations_version") != VERSION
            or len(rows) != 1 or rows[0].get("name") != source["name"] or rows[0].get("tag") != source["tag"]
            or rows[0].get("state") != "completed" or rows[0].get("complete") is not True
            or rows[0].get("matched") is not True or rows[0].get("expectations_complete") is not True or rows[0].get("skipped")):
        fail("not_ready", "기대 판정이 전체 테스트에서 일치한 완료된 단일 검증만 풀이에 추가할 수 있습니다.")
    sets = selected_testsets(job.document, job.options, job.kind)
    indices = job.options.get("test_indices", [])
    planned = {(name, test["index"]) for name in sets for test in expanded_tests(job.document["testsets"][name])
               if not indices or test["index"] in indices}
    prepared = result.get("testsets", {})
    cases = rows[0].get("cases", [])
    observed = [(case.get("testset"), case.get("index")) for case in cases]
    prepared_keys = [(name, test.get("index")) for name, tests in prepared.items() for test in tests]
    scope = job.options.get("scope", {})
    if (not planned or set(prepared) != set(sets) or len(prepared_keys) != len(planned) or set(prepared_keys) != planned
            or len(observed) != len(planned) or set(observed) != planned
            or scope.get("solutions") != [source["name"]] or scope.get("test_count") != len(planned)
            or any(case.get("verdict") not in VERDICTS for case in cases)):
        fail("incomplete_verification", "저장된 채점 결과가 요청한 테스트 범위 전체를 포함하지 않습니다.")
    inputs = {(name, test["index"]): (test.get("input_sha"), test.get("answer_sha")) for name, tests in prepared.items() for test in tests}
    if any(not all(inputs[key]) for key in planned) or any(
            (case.get("input_sha"), case.get("answer_sha")) != inputs[case["testset"], case["index"]] for case in cases):
        fail("incomplete_verification", "실행한 입력·정답과 저장된 채점 결과가 일치하지 않습니다.")
    expected = evaluate(job.document, source, prepared, cases)
    if not expected["expectations_complete"] or not expected["matched"] or any(
            item["matched"] is not True for item in expected["expectations"]):
        fail("not_ready", "기대 판정이 전체 테스트에서 일치한 단일 검증만 풀이에 추가할 수 있습니다.")


def locked_context(db, job, user):
    problem_id, job_id, user_id = job.problem_id, job.id, user.id
    problem, _ = authorize(db, problem_id, user, permission="edit_solutions")
    workspace(db, problem, user)  # May create/commit; acquire locks afterwards.
    from .folders import lock_user
    lock_user(db, user)
    # Apply and discard use the same lock order. Re-read permissions and all
    # terminal state after waiting for another click/transaction to finish.
    db.expire_all()
    authorize(db, problem_id, user, permission="edit_solutions")
    wc = db.scalar(select(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user_id)
                   .with_for_update().execution_options(populate_existing=True))
    saved = db.scalar(select(Job).where(Job.id == job_id, Job.problem_id == problem_id).execution_options(populate_existing=True))
    if not saved:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    return wc, saved


def response(wc, source, application):
    return {"version": wc.version, "document_hash": document_hash(wc.document), "document": wc.document,
            "application": application_json(application), "solution": source}


def apply_solution(db, job, user, version):
    wc, job = locked_context(db, job, user)
    source = saved_source(job)
    previous = db.get(JobApplication, (job.id, user.id))
    if previous:
        return response(wc, source, previous)
    if db.get(JobDiscard, (job.id, user.id)):
        fail("result_discarded", "버린 단일 검증 코드는 추가할 수 없습니다. 다시 검증하세요.", 409)
    if wc.version != version:
        fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
    if job.options.get("source_document_hash") != document_hash(wc.document):
        fail("stale_results", "검증 이후 문제 자료가 변경되었습니다. 현재 자료로 다시 검증하세요.", 409)
    if source["name"] in wc.document["files"] or source["name"] in wc.document["solutions"]:
        fail("solution_name_conflict", "같은 이름의 파일이나 풀이가 있습니다. 덮어쓰지 않고 추가를 중단했습니다.", 409)
    complete_match(job, source)
    try:
        content = read_blob(source["sha256"])
    except (OSError, ValueError):
        fail("invalid_solution_blob", "검증한 코드 파일을 읽을 수 없습니다. 다시 검증하세요.", 409)
    if len(content) != source["size"] or hashlib.sha256(content).hexdigest() != source["sha256"]:
        fail("invalid_solution_blob", "검증한 코드 파일이 변경되었습니다. 다시 검증하세요.", 409)
    doc = copy.deepcopy(wc.document)
    doc["solutions"][source["name"]] = source
    doc = validate_document(db, job.problem_id, doc)
    next_version = version + 1
    updated = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == job.problem_id,
        WorkingCopy.user_id == user.id, WorkingCopy.version == version).values(document=doc, version=next_version, updated_at=now()))
    if not updated.rowcount:
        db.rollback()
        fail("workspace_changed", "다른 창에서 변경되었습니다. 최신 작업 사본을 불러오세요.", 409)
    application = JobApplication(job_id=job.id, user_id=user.id, workspace_version=next_version, document_hash=document_hash(doc))
    db.add(application)
    db.commit()
    db.refresh(wc)
    return response(wc, source, application)


def discard_solution(db, job, user):
    _, job = locked_context(db, job, user)
    if not is_single(job) or job.status not in {"completed", "failed", "cancelled"}:
        fail("not_ready", "실행이 끝난 단일 검증 코드만 버릴 수 있습니다.")
    if db.get(JobApplication, (job.id, user.id)):
        fail("already_applied", "이미 풀이에 추가한 코드입니다. 풀이 목록에서 수정해 주세요.", 409)
    previous = db.get(JobDiscard, (job.id, user.id))
    if previous:
        return {"discarded_at": previous.discarded_at}
    row = JobDiscard(job_id=job.id, user_id=user.id)
    db.add(row)
    db.commit()
    return {"discarded_at": row.discarded_at}
