import copy
import hashlib
import re
from typing import Annotated, Literal
from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session, load_only
from .config import get_settings
from .auth import current_user
from .db import Job, JobApplication, JobComment, Revision, User, WorkingCopy, now, session_dependency
from .domain import authorize, cautions, document_hash, expanded_tests, fail, parse_script, validate_document, workspace
from .schema import Strict, SourceFile, Info
from .storage import put_blob
from .execution_limits import MAX_TESTS_PER_SET, MAX_STRESS_ITERATIONS, MAX_ACTIVE_JOBS_PER_USER, MAX_STRESS_PATTERNS, MAX_STRESS_COUNTEREXAMPLES, MAX_MINIMIZE_ATTEMPTS, MAX_MINIMIZE_SECONDS
from .prepared_results import application_json, apply_prepared_results, pending_preparations

router = APIRouter(prefix="/api/problems", tags=["execution"])


@router.get("/{problem_id}/preparation-status")
def preparation_status(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, access = authorize(db, problem_id, user)
    wc = workspace(db, problem, user)
    editable = access.role in {"OWNER", "WRITE"}
    return {"version": wc.version, "pending_preparations":
            pending_preparations(db, problem_id, user.id, document_hash(wc.document)) if editable else []}


class TemporarySolution(Strict):
    name: str = Field(min_length=1, max_length=100)
    language: Literal["cpp17", "cpp20", "c17", "c99", "python3", "java17", "java8"] = "python3"
    tag: Literal["OK", "RJ", "TL", "TO", "TM", "WA", "PE", "ML", "NR", "RE"] = "OK"
    content: str = Field(min_length=1, max_length=1048576)

    @field_validator("name")
    @classmethod
    def safe_temporary_name(cls, value):
        return SourceFile.check_name(value)


class StressPattern(Strict):
    name: str = Field(default="", max_length=80)
    command: str = Field(min_length=1, max_length=4096)

    @field_validator("name")
    @classmethod
    def pattern_name(cls, value):
        return Info.single_line(value).strip()


class JobOptions(Strict):
    name: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=8000)
    auto_apply: bool = False
    force_prepare: bool = False
    execution_backend: Literal["editor", "zoj"] | None = None
    testset: str = "tests"
    all_testsets: bool = False
    snapshot: Literal["working_copy", "committed"] | None = None
    temporary_solutions: list[TemporarySolution] = Field(default_factory=list, max_length=10)
    solutions: list[str] = Field(default_factory=list, max_length=200)
    test_indices: list[Annotated[int, Field(strict=True, ge=1, le=MAX_TESTS_PER_SET)]] = Field(default_factory=list, max_length=MAX_TESTS_PER_SET)
    generator: str = Field(default="", max_length=4096)
    patterns: list[StressPattern] = Field(default_factory=list, max_length=MAX_STRESS_PATTERNS)
    seed_start: int = Field(default=1, ge=0, le=9007199254740991, strict=True)
    seed_step: int = Field(default=1, ge=1, le=9007199254740991, strict=True)
    group: str = Field(default="", max_length=64)
    reference: str = ""
    candidate: str = ""
    candidates: list[str] = Field(default_factory=list, max_length=200)
    iterations: int = Field(default=50, ge=1, le=MAX_STRESS_ITERATIONS)
    duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    stop_on_first: bool = True
    stop_mode: Literal["first", "target", "all_candidates"] | None = None
    focus_uncovered: bool = False
    minimize: bool = False
    minimize_attempts: int = Field(default=40, ge=1, le=MAX_MINIMIZE_ATTEMPTS, strict=True)
    minimize_seconds: int = Field(default=10, ge=1, le=MAX_MINIMIZE_SECONDS, strict=True)
    counterexample_limit: Literal[1, 5, 10] = 1
    counterexample_verdicts: list[Literal["WA", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT", "PARTIAL"]] = Field(default_factory=lambda: ["WA", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT", "PARTIAL"], min_length=1, max_length=8)
    verify: bool = True
    prepare_mode: Literal["full", "inputs", "validate", "answers"] = "full"
    package_type: Literal["linux", "sources"] = "linux"
    export_format: Literal["editor", "zoj", "linux"] = "editor"
    statement_language: str = Field(default="korean", min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def invocation_name(cls, value):
        return Info.single_line(value).strip()


class JobRequest(Strict):
    kind: Literal["prepare", "verify", "validator-tests", "checker-tests", "stress", "package", "render"]
    version: int = Field(ge=0)
    revision: int | None = Field(default=None, ge=0)
    options: JobOptions = Field(default_factory=JobOptions)
    reuse: bool = True


def job_json(job, include_result=True, application=None):
    result = job.result if include_result else {"summary": job.result.get("summary", ""), "progress": job.result.get("progress", 0)}
    if include_result and job.status not in {"queued", "running"} and result.get("solutions") and "test_matrix" not in result:
        from .test_matrix import analyze
        result = {**result, "test_matrix": analyze(job.document, result)}
    if include_result and job.kind == "stress" and job.status not in {"queued", "running"} and result.get("counterexamples") and "coverage" not in result:
        from .stress import minimum_cover
        result = {**result, "coverage": minimum_cover(result["counterexamples"])}
    return {"id": job.id, "problem_id": job.problem_id, "kind": job.kind, "status": job.status,
            "document_hash": job.document_hash, "workspace_version": job.workspace_version,
            "revision": job.revision, "options": job.options, "created_at": job.created_at, "requested_by": job.user_id,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "application": application_json(application),
            "result": result}


@router.post("/{problem_id}/jobs", status_code=202)
def create_job(problem_id: str, values: JobRequest, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    return _create_job(problem_id, values, user, db)


def _create_job(problem_id: str, values: JobRequest, user: User, db: Session, *, from_committed_revision: bool = False):
    problem, access = authorize(db, problem_id, user)
    writer = access.role in {"WRITE", "OWNER"}
    translator_render = values.kind == "render" and access.translator and not access.reviewer
    if not writer and not access.reviewer and not translator_render:
        fail("forbidden", "실행에는 쓰기 권한 또는 검수자 권한이 필요합니다.", 403)
    if not writer and values.kind in {"prepare", "package"}:
        fail("forbidden", "검수자는 입력·정답 생성이나 패키지 생성을 할 수 없습니다.", 403)
    options = values.options.model_dump()
    if not writer and options["auto_apply"]:
        fail("forbidden", "검수 결과는 문제 자료에 자동 반영할 수 없습니다.", 403)
    if not writer and not translator_render and options["snapshot"] == "working_copy":
        fail("forbidden", "검수자는 커밋된 리비전으로만 실행할 수 있습니다.", 403)
    snapshot = options["snapshot"] or ("committed" if not writer and not translator_render else "working_copy")
    if values.kind == "package" or from_committed_revision:
        snapshot = "committed"
    wc = workspace(db, problem, user) if writer or translator_render else None
    if wc and wc.version != values.version and (snapshot == "working_copy" or (values.kind == "package" and not from_committed_revision)):
        fail("workspace_changed", "최신 작업 사본을 저장한 뒤 실행하세요.", 409)
    revision = problem.revision
    if snapshot == "committed":
        revision = values.revision if values.revision is not None else problem.revision
        committed = db.get(Revision, (problem_id, revision))
        if not committed:
            fail("invalid_revision", "커밋된 리비전을 찾을 수 없습니다.", 404)
        doc = copy.deepcopy(committed.document)
    else:
        doc = copy.deepcopy(wc.document)
    if values.kind == "package" and wc.document != problem.document and not from_committed_revision:
        fail("commit_required", "패키지를 만들기 전에 변경사항을 커밋하세요.", 409)
    options.update(snapshot=snapshot, source_document_hash=document_hash(doc), source_revision=revision,
                   permission_mode="write" if writer else "translator" if translator_render else "reviewer")
    temporary = []
    if options["temporary_solutions"] and values.kind not in {"verify", "stress"}:
        fail("invalid_options", "임시 검수 코드는 풀이 검증이나 스트레스 테스트에서 실행하세요.")
    for source in values.options.temporary_solutions:
        if source.name in doc["files"] or source.name in doc["solutions"]:
            fail("temporary_name_conflict", "임시 코드는 기존 문제 파일과 다른 이름을 사용하세요.")
        content = source.content.encode()
        if len(content) > 1048576:
            fail("too_large", "임시 검수 코드는 파일당 1MiB 이하여야 합니다.", 413)
        metadata = SourceFile(name=source.name, language=source.language, sha256=hashlib.sha256(content).hexdigest(), size=len(content)).model_dump()
        doc["solutions"][source.name] = {**metadata, "tag": source.tag, "extra_tags": {}}
        temporary.append((metadata, content))
    options["temporary_solutions"] = [{**metadata, "tag": doc["solutions"][metadata["name"]]["tag"]} for metadata, _ in temporary]
    if options["all_testsets"] and values.kind not in {"prepare", "package"}:
        fail("invalid_options", "전체 세트 생성은 입력·정답 생성에서 선택하세요.")
    if options["auto_apply"] and values.kind != "prepare":
        fail("invalid_options", "자동 반영은 입력·정답 생성에서만 사용할 수 있습니다.")
    if options["force_prepare"] and values.kind != "verify":
        fail("invalid_options", "준비 결과 재사용 해제는 풀이 검증에서 선택하세요.")
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
    options["solutions"] = list(dict.fromkeys(options["solutions"]))
    options["test_indices"] = sorted(set(options["test_indices"]))
    if options["test_indices"] and values.kind not in {"verify", "prepare"}:
        fail("invalid_options", "테스트 번호 범위는 풀이 검증·테스트 준비에서 선택하세요.")
    if values.kind != "prepare" and options["prepare_mode"] != "full":
        fail("invalid_options", "부분 생성 모드는 테스트 준비에서만 사용하세요.")
    if values.kind == "prepare":
        if options["test_indices"] and options["all_testsets"]:
            fail("invalid_options", "선택 번호는 한 테스트 세트에서 지정하세요.")
        sets = doc["testsets"].values() if options["all_testsets"] else [doc["testsets"][options["testset"]]]
        if (options["test_indices"] or options["prepare_mode"] != "full") and any(config["script"].strip() for config in sets):
            fail("materialize_first", "전체 입력·정답을 생성하고 반영하여 생성 규칙을 등록 테스트로 확정한 뒤 부분 작업을 실행하세요.")
        if options["test_indices"]:
            available = {test["index"] for test in expanded_tests(doc["testsets"][options["testset"]])}
            if set(options["test_indices"]) - available:
                fail("invalid_test_indices", "현재 세트에 없는 테스트 번호입니다.")
    for name in [*options["solutions"], *options["candidates"], options["reference"], options["candidate"]]:
        if name and name not in doc["solutions"]:
            fail("invalid_solution", "풀이 파일을 찾을 수 없습니다.")
    if values.kind == "verify":
        tests = expanded_tests(doc["testsets"][options["testset"]])
        available = {test["index"] for test in tests}
        unknown = set(options["test_indices"]) - available
        if unknown:
            fail("invalid_test_indices", "선택한 세트에 없는 테스트 번호입니다.", indices=sorted(unknown))
        selected_solutions = options["solutions"] or list(doc["solutions"])
        runnable = [name for name in selected_solutions if doc["solutions"][name]["tag"] != "NR"]
        if not tests or not runnable:
            fail("empty_scope", "실행할 테스트와 NR 이외의 풀이를 선택하세요.")
        options["scope"] = {"testset": options["testset"], "solutions": runnable,
                            "test_indices": options["test_indices"],
                            "test_count": len(options["test_indices"]) if options["test_indices"] else len(tests)}
    if values.kind == "stress":
        options["candidates"] = list(dict.fromkeys(options["candidates"] or ([options["candidate"]] if options["candidate"] else [])))
        if not options["candidates"] or not (options["generator"].strip() or options["patterns"]):
            fail("invalid_stress", "생성기 명령과 비교할 풀이를 선택하세요.")
        main = next((name for name, source in doc["solutions"].items() if source["tag"] == "MA"), "")
        if (options["reference"] or main) in options["candidates"]:
            fail("invalid_stress", "기준 풀이와 비교할 풀이는 서로 달라야 합니다.")
        if options["duration_seconds"] and options["duration_seconds"] > min(get_settings().stress_max_seconds, get_settings().job_timeout_seconds):
            fail("invalid_stress_duration", "실행 시간이 서버의 작업 한도를 넘습니다.")
        from .stress import stress_configuration, validate_patterns, stop_mode
        try:
            options["patterns"] = validate_patterns(doc, options)
        except ValueError as error:
            fail("invalid_stress_pattern", str(error))
        options["patterns"] = [{**pattern, "name": pattern["name"] or f"패턴 {index + 1}"} for index, pattern in enumerate(options["patterns"])]
        options["generator"] = options["patterns"][0]["command"]
        options["stop_mode"] = stop_mode(options)
        options["stop_on_first"] = options["stop_mode"] == "first"
        if options["stop_mode"] == "all_candidates":
            options["focus_uncovered"] = True
        if options["seed_start"] + (options["iterations"] - 1) * options["seed_step"] > 9007199254740991:
            fail("invalid_stress_seed", "마지막 시드가 안전한 정수 범위를 넘습니다.")
        if any(doc["solutions"][name]["tag"] == "NR" for name in [*options["candidates"], options["reference"] or main] if name):
            fail("invalid_stress", "NR 풀이를 스트레스 테스트 대상으로 실행할 수 없습니다.")
        if not (options["reference"] or main):
            fail("invalid_stress", "기준 풀이 또는 Main 풀이를 지정하세요.")
        if options["group"] and (not doc["testsets"][options["testset"]]["groups_enabled"] or options["group"] not in doc["testsets"][options["testset"]]["groups"]):
            fail("invalid_stress_group", "선택한 테스트 세트에서 사용 중인 그룹을 지정하세요.")
        if options["minimize"] and doc["info"]["interactive"]:
            fail("invalid_stress_minimize", "자동 축소는 비인터랙티브 입력에서 사용할 수 있습니다.")
        options["stress_configuration"] = stress_configuration(doc, options)
    if values.kind == "package" and options["export_format"] == "linux":
        if options["execution_backend"] not in {None, "editor"}:
            fail("invalid_replay_backend", "Linux 재현 패키지는 Editor 격리 실행을 선택하세요. ZOJ 대회 채점기의 언어별 보정을 재현했다고 표시하지 않습니다.")
        options["execution_backend"] = "editor"
    options["execution_backend"] = options["execution_backend"] or get_settings().judge_backend
    if values.kind not in {"verify", "package"} or (values.kind == "package" and not options["verify"]):
        options["execution_backend"] = "editor"
    if options["execution_backend"] == "zoj":
        from .judge_bridge import compatible, capabilities
        compatible(doc, options, values.kind)
        options["judge_revision"] = capabilities(user)["runtime_revision"]
    options["execution_transport"] = get_settings().execution_transport
    if options["execution_transport"] == "agent":
        from .judge_bridge import capabilities
        execution = capabilities(user).get("execution", {})
        if execution.get("protocol") != "editor-execute-v1" or execution.get("online_nodes", 0) < 1:
            fail("execution_agents_unavailable", "Editor 실행이 허용된 채점 에이전트가 준비되지 않았습니다. 관리자에게 문의하세요.", 503)
        options["execution_revision"] = execution["runtime_revision"]
    hash_value = document_hash(doc)
    if values.reuse and not options["force_prepare"]:
        cached = db.scalars(select(Job).where(Job.problem_id == problem_id, Job.document_hash == hash_value, Job.kind == values.kind, Job.status.in_(["queued", "running", "completed"])).order_by(Job.created_at.desc())).all()
        for row in cached:
            if row.options == options:
                return job_json(row, application=db.get(JobApplication, (row.id, user.id)))
    active = db.scalars(select(Job).where(Job.user_id == user.id, Job.status.in_(["queued", "running"]))).all()
    if len(active) >= MAX_ACTIVE_JOBS_PER_USER:
        fail("too_many_jobs", f"동시에 요청할 수 있는 검증 작업은 {MAX_ACTIVE_JOBS_PER_USER}개입니다.", 429)
    for _, content in temporary:
        put_blob(db, problem_id, content)
    row = Job(problem_id=problem_id, user_id=user.id, kind=values.kind, document=doc, document_hash=hash_value,
              workspace_version=wc.version if wc else values.version, revision=revision, options=options)
    db.add(row)
    db.commit()
    return job_json(row)


@router.get("/{problem_id}/jobs")
def list_jobs(problem_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    rows = db.execute(select(Job, JobApplication).outerjoin(JobApplication,
        (JobApplication.job_id == Job.id) & (JobApplication.user_id == user.id))
        .options(load_only(Job.id, Job.problem_id, Job.user_id, Job.kind, Job.status, Job.document_hash, Job.workspace_version,
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
    _, access = authorize(db, problem_id, user)
    row = db.get(Job, job_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    if access.role not in {"WRITE", "OWNER"} and not (access.reviewer and row.user_id == user.id):
        fail("forbidden", "검수자는 자신이 요청한 실행만 취소할 수 있습니다.", 403)
    db.execute(update(Job).where(Job.id == job_id, Job.status.in_(["queued", "running"])).values(status="cancelled", finished_at=now()))
    db.commit()
    db.refresh(row)
    return job_json(row)


class InvocationComment(Strict):
    content: str = Field(min_length=1, max_length=8000)


@router.get("/{problem_id}/jobs/{job_id}/comments")
def list_comments(problem_id: str, job_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    rows = db.execute(select(JobComment, User.display_name).join(User, User.id == JobComment.user_id)
                      .where(JobComment.job_id == job_id).order_by(JobComment.created_at, JobComment.id).limit(1000))
    return [{"id": note.id, "author": name, "content": note.content, "created_at": note.created_at} for note, name in rows]


@router.post("/{problem_id}/jobs/{job_id}/comments", status_code=201)
def add_comment(problem_id: str, job_id: str, values: InvocationComment, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    _, access = authorize(db, problem_id, user)
    if access.role not in {"WRITE", "OWNER"} and not access.reviewer:
        fail("forbidden", "의견 작성에는 쓰기 권한 또는 검수자 권한이 필요합니다.", 403)
    job = db.scalar(select(Job).where(Job.id == job_id, Job.problem_id == problem_id).with_for_update())
    if not job:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    if not values.content.strip():
        fail("empty_comment", "검수 의견을 입력하세요.")
    if db.scalar(select(func.count()).select_from(JobComment).where(JobComment.job_id == job_id)) >= 1000:
        fail("comment_limit", "이 실행에 보관할 수 있는 의견 한도에 도달했습니다.", 429)
    row = JobComment(job_id=job_id, user_id=user.id, content=values.content.strip())
    db.add(row)
    db.commit()
    return {"id": row.id, "author": user.display_name, "content": row.content, "created_at": row.created_at}


class ApplyResults(Strict):
    version: int = Field(ge=0)


class ApplyCounterexamples(ApplyResults):
    testset: str
    group: str | None = Field(default=None, max_length=64)
    use_minimized: bool = False
    counterexample_ids: list[str] = Field(min_length=1, max_length=MAX_STRESS_COUNTEREXAMPLES)

    @field_validator("counterexample_ids")
    @classmethod
    def valid_ids(cls, value):
        if len(value) != len(set(value)) or any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in value):
            raise ValueError("Use unique counterexample identifiers")
        return value


@router.post("/{problem_id}/jobs/{job_id}/counterexamples/apply")
def apply_counterexamples(problem_id: str, job_id: str, values: ApplyCounterexamples, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, write=True)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    if job.kind != "stress" or job.status not in {"completed", "cancelled", "failed"}:
        fail("not_ready", "종료된 스트레스 테스트의 유효한 반례만 추가할 수 있습니다.")
    wc = workspace(db, problem, user)
    db.refresh(wc, with_for_update=True)
    if wc.version != values.version:
        fail("workspace_changed", "다른 창의 변경을 불러온 뒤 다시 추가하세요.", 409)
    previous = db.get(JobApplication, (job.id, user.id))
    expected_hash = previous.document_hash if previous else (job.options.get("source_document_hash") or job.document_hash)
    if expected_hash != document_hash(wc.document):
        fail("stale_results", "문제 자료가 변경되었습니다. 현재 자료로 스트레스 테스트를 다시 실행하세요.", 409)
    if values.testset not in wc.document["testsets"]:
        fail("invalid_testset", "추가할 테스트 세트를 찾을 수 없습니다.")
    examples = job.result.get("counterexamples") or ([job.result["counterexample"]] if job.result.get("counterexample") else [])
    by_id = {item.get("id", item["input_sha"][:32]): item for item in examples}
    if any(identifier not in by_id for identifier in values.counterexample_ids):
        fail("invalid_counterexample", "이 실행에서 검증한 반례를 선택하세요.")
    doc = copy.deepcopy(wc.document)
    testset = doc["testsets"][values.testset]
    group = values.group if values.group is not None else (job.options.get("group", "") if values.testset == job.options.get("testset") else "")
    if group and (not testset["groups_enabled"] or group not in testset["groups"]):
        fail("invalid_test_group", "추가할 테스트 세트에 존재하는 그룹을 선택하세요.")
    occupied = {test["index"] for test in testset["tests"].values()}
    occupied.update(test["index"] for test in parse_script(testset["script"], occupied))
    existing = {test["input_sha"] for test in testset["tests"].values() if test["input_sha"]}
    chosen, duplicates = [], 0
    for identifier in values.counterexample_ids:
        example = by_id[identifier]
        if values.use_minimized and example.get("minimized"):
            reduced = example["minimized"]
            original_pairs = {(item["solution"], item["verdict"]) for item in example.get("findings", [])}
            reduced_pairs = {(item["solution"], item["verdict"]) for item in reduced.get("findings", [])}
            if not original_pairs or not original_pairs <= reduced_pairs or not reduced.get("input_sha") or not reduced.get("answer_sha"):
                fail("invalid_minimized_counterexample", "모든 원래 판정이 재검증된 축소 반례만 추가할 수 있습니다.")
            example = {**example, **reduced}
        if example["input_sha"] in existing:
            duplicates += 1
            continue
        existing.add(example["input_sha"])
        chosen.append(example)
    free = [index for index in range(1, MAX_TESTS_PER_SET + 1) if index not in occupied]
    if len(chosen) > len(free):
        fail("test_limit", "생성 스크립트까지 포함해 세트당 테스트 한도를 넘습니다.")
    indices = free[:len(chosen)]
    for example, index in zip(chosen, indices):
        key = str(index)
        while key in testset["tests"]:
            key = "stress-" + key
        testset["tests"][key] = {"index": index, "input_sha": example["input_sha"], "answer_sha": example["answer_sha"],
                               "command": "", "group": group, "points": 0, "sample": False,
                               "description": (f"스트레스 반례 · 시드 {example.get('seed', example['iteration'])} · " + ", ".join(sorted({finding['solution'] + ':' + finding['verdict'] for finding in example.get('findings', [])})))[:200]}
    doc = validate_document(db, problem_id, doc)
    version = values.version + bool(chosen)
    updated = db.execute(update(WorkingCopy).where(WorkingCopy.problem_id == problem_id, WorkingCopy.user_id == user.id,
                         WorkingCopy.version == values.version).values(document=doc, version=version, updated_at=now()))
    if not updated.rowcount:
        db.rollback()
        fail("workspace_changed", "다른 창에서 변경되었습니다. 다시 불러오세요.", 409)
    if previous:
        previous.applied_at, previous.workspace_version, previous.document_hash = now(), version, document_hash(doc)
    else:
        db.add(JobApplication(job_id=job.id, user_id=user.id, workspace_version=version, document_hash=document_hash(doc)))
    db.commit()
    return {"version": version, "added_count": len(chosen), "duplicate_count": duplicates, "indices": indices,
            "group": group, "needs_context_validation": values.testset != job.options.get("testset", "tests") or group != job.options.get("group", "")}


@router.post("/{problem_id}/jobs/{job_id}/apply")
def apply_prepared(problem_id: str, job_id: str, values: ApplyResults, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user, write=True)
    row = db.get(Job, job_id)
    if not row or row.problem_id != problem_id:
        fail("not_found", "실행 기록을 찾을 수 없습니다.", 404)
    return apply_prepared_results(db, row, user, values.version)
