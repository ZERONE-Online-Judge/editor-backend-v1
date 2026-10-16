"""Final solution verification and preparation on authorized judge agents."""
import base64
import hashlib
import json
import tempfile
import time
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from .auth import current_user
from .config import get_settings
from .db import SessionLocal, User, Job, now, session_dependency
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .execution_limits import public_limits
from .domain import fail
from .parallel import execution_budget
from .sandbox import SandboxError, VENDOR
from .storage import read_blob
from .invocation_scope import selected_testsets
from .zoj import remote
from .judge_requests import request_with_retry

router = APIRouter(prefix="/api", tags=["judge environment"])
LANGUAGES = {"cpp17": "cpp17", "python3": "python313", "c99": "c99", "java8": "java8"}
VERDICTS = {"accepted": "AC", "wrong_answer": "WA", "compile_error": "CE", "runtime_error": "RE",
            "time_limit_exceeded": "TLE", "memory_limit_exceeded": "MLE", "output_limit_exceeded": "OLE",
            "system_error": "JUDGE_ERROR", "cancelled": "JUDGE_ERROR"}
ACTIVE = {"uploading", "waiting", "preparing", "judging"}


def capabilities(user):
    result = remote("GET", "/api/editor/judge/capabilities", user, timeout=15)
    if result.get("backend") != "zoj" or not isinstance(result.get("runtime_revision"), str):
        fail("judge_protocol_invalid", "ZOJ 채점 연결의 응답 형식이 올바르지 않습니다.", 502)
    return result


@router.get("/execution-environment")
def environment(user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    settings = get_settings()
    result = {"default_backend": settings.judge_backend, "zoj_configured": bool(settings.zoj_api_url), "zoj": None, "execution_transport": settings.execution_transport}
    result["limits"] = public_limits(settings)
    counts = dict(db.execute(select(Job.status, func.count()).where(Job.status.in_(["running", "queued"])).group_by(Job.status)).all())
    result["capacity"] = {"running_jobs": counts.get("running", 0), "queued_jobs": counts.get("queued", 0)}
    result["updated_at"] = now()
    if settings.zoj_api_url:
        try:
            result["zoj"] = capabilities(user)
        except HTTPException as error:
            result["error"] = error.detail.get("message", "ZOJ 채점 환경을 확인하지 못했습니다.")
    return result


def compatible(doc, options, kind):
    info = doc["info"]
    issues = []
    if info["interactive"] or info["input_file"] != "stdin" or info["output_file"] != "stdout":
        issues.append("현재 ZOJ 채점기는 표준 입출력의 비대화형 문제를 지원합니다.")
    if info["points_enabled"]:
        issues.append("부분 점수는 Editor 격리 실행을 선택해 검증하세요.")
    if not 100 <= info["time_limit_ms"] <= 10000 or not 16 <= info["memory_limit_mb"] <= 1024:
        issues.append("ZOJ 문제 제한 범위는 100~10,000ms, 16~1,024MiB입니다.")
    if any(f.get("compile_with_solution") for f in doc["files"].values()):
        issues.append("grader와 결합하는 풀이는 Editor 격리 실행으로 검증하세요.")
    if any(f.get("resource_policy") for f in doc["files"].values()):
        issues.append("언어·대상별 리소스 정책은 Editor 격리 실행으로 검증하세요. 에이전트 전송 모드에서는 같은 채점 노드가 격리 실행을 처리합니다.")
    selected = list(doc["solutions"]) if kind == "package" else options["solutions"] or list(doc["solutions"])
    for name in selected:
        s = doc["solutions"][name]
        if s["tag"] == "NR":
            continue
        if s["language"] not in LANGUAGES:
            issues.append(f"{name}: ZOJ 지원 언어는 C++17·C99·Python 3.13·Java 8입니다. 언어 표준을 자동으로 바꾸지 않습니다.")
        if s["tag"] == "PE" or "PE" in s.get("extra_tags", {}).values() or any("PE" in tags.values() for tags in s.get("group_tags", {}).values()):
            issues.append(f"{name}: PE 기대 판정은 Editor 격리 실행을 선택해 검증하세요.")
    checker = doc["files"].get(doc["checker"])
    if checker and checker["language"] not in {"cpp17", "c99", "python3"}:
        issues.append("ZOJ checker는 C++17·C99·Python 소스로 검증하세요.")
    if issues:
        fail("judge_incompatible", " ".join(issues), 400, issues=issues)


def archive(pipeline, name, set_name, tests, attempt):
    source = pipeline.doc["solutions"][name]
    blobs = {source["sha256"]: read_blob(source["sha256"])}
    resources = []
    resource_names = set()
    def resource(filename, value, role):
        sha = hashlib.sha256(value).hexdigest()
        blobs[sha] = value
        resources.append({"name": filename, "role": role, "sha256": sha})
        resource_names.add(filename)
    checker = pipeline.doc["checker"]
    if checker.startswith("std::"):
        filename = checker.removeprefix("std::") + ".cpp"
        checker_name = "std-" + filename
        while checker_name in pipeline.doc["files"]:
            checker_name = "_" + checker_name
        resource(checker_name, (VENDOR / filename).read_bytes(), "checker")
    else:
        resource(checker, read_blob(pipeline.doc["files"][checker]["sha256"]), "checker")
    resource("testlib.h", (VENDOR / "testlib.h").read_bytes(), "package-resource")
    for filename, f in pipeline.doc["files"].items():
        if (f["kind"] == "resource" or f["kind"] == "aux" and f.get("attach_to_build") is not False) and filename not in resource_names:
            resource(filename, read_blob(f["sha256"]), "package-resource")
    cases = []
    for t in tests:
        for sha in (t["input_sha"], t["answer_sha"]):
            blobs[sha] = read_blob(sha)
        cases.append({"index": t["index"], "input_sha": t["input_sha"], "answer_sha": t["answer_sha"]})
    operation = hashlib.sha256(json.dumps([pipeline.job.id, name, set_name, [t["index"] for t in tests], attempt], separators=(",", ":")).encode()).hexdigest()[:32]
    manifest = {"format": "zoj-editor-judge-v1", "operation_id": operation,
                "collect_all_results": True,
                "project_id": pipeline.job.problem_id, "context_hash": pipeline.job.document_hash,
                "source": {"name": name, "language": LANGUAGES[source["language"]], "sha256": source["sha256"]},
                "time_limit_ms": pipeline.doc["info"]["time_limit_ms"], "memory_limit_mb": pipeline.doc["info"]["memory_limit_mb"],
                "resources": resources, "testcases": cases}
    path = pipeline.sandbox.root / (operation + ".judge.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def write(filename, content):
            entry = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, content)
        write("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        for sha, content in sorted(blobs.items()):
            write("blobs/" + sha, content)
    if path.stat().st_size > get_settings().max_archive_bytes or sum(map(len, blobs.values())) > 256 * 1024**2:
        path.unlink()
        raise SandboxError("ZOJ 채점 자료는 ZIP 128MiB·전체 원본 256MiB 이하여야 합니다.")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while part := f.read(1048576):
            digest.update(part)
    return path, digest.hexdigest()


def run_batch(pipeline, user, name, set_name, tests, attempt, cancellation):
    path, digest = archive(pipeline, name, set_name, tests, attempt)
    endpoint = "/api/editor/judge/runs"
    run_id = None
    done = False
    attempted = False
    def check():
        pipeline.sandbox.check_cancelled()
        if cancellation.is_set():
            raise SandboxError("다른 풀이 검증이 실패하여 함께 실행하던 작업을 중단했습니다.")

    def request(method, endpoint, *args, **kwargs):
        return request_with_retry(remote, method, endpoint, user, *args, check=check,
            on_wait=lambda: pipeline.event(f"ZOJ 연결 대기 · {name} / {set_name} · 같은 요청을 재시도합니다."), **kwargs)

    try:
        check()
        attempted = True
        run = request("POST", endpoint, path, digest)
        run_id = run["id"]
        last_progress = None
        while run["status"] in ACTIVE:
            check()
            progress = (run["status"], run["progress_current"], run["progress_total"])
            if progress != last_progress:
                pipeline.event(f"ZOJ 채점 · {name} / {set_name} · {run['progress_current']}/{run['progress_total']}", node=run.get("node_name"), judge_run_id=run_id)
                last_progress = progress
            if getattr(pipeline, "verification", None):
                pipeline.verification.batch(name, set_name, observed_cases(pipeline, user, set_name, tests, run))
            time.sleep(1)
            run = request("GET", "/api/editor/judge/runs/" + run_id, timeout=15)
        pipeline.sandbox.check_cancelled()
        if run["status"] not in VERDICTS or not run.get("result"):
            raise SandboxError("ZOJ 채점기가 최종 판정을 반환하지 않았습니다.")
        done = True
        rows = observed_cases(pipeline, user, set_name, tests, run)
        return {"id": run_id, "testset": set_name, "status": run["status"], "node_name": run.get("node_name"),
                "agent_version": run.get("agent_version"), "runtime_revision": run["runtime_revision"],
                "testcase_count": len(tests), **run["result"], "cases": rows}
    finally:
        if path:
            path.unlink(missing_ok=True)
        if attempted and not done:
            try:
                endpoint = "/api/editor/judge/runs/" + run_id if run_id else "/api/editor/judge/run-operations/" + path.stem
                remote("DELETE", endpoint, user, timeout=15)
            except Exception:
                pass


def observed_cases(pipeline, user, set_name, tests, run):
    lookup = {test["index"]: test for test in tests}
    result = []
    seen = set()
    cache = getattr(pipeline, "judge_outputs", None)
    if cache is None:
        cache = pipeline.judge_outputs = {}
    for record in (run.get("result") or {}).get("testcase_results", []):
        index = record["index"]
        if index not in lookup or index in seen or record["final_status"] not in VERDICTS:
            raise SandboxError("ZOJ 채점기의 개별 테스트 결과가 요청 범위와 다릅니다.")
        seen.add(index)
        test = lookup[index]
        actual_sha = None
        output_error = None
        if record.get("output_available"):
            key = (run["id"], index, record["output_sha256"])
            if key not in cache:
                try:
                    output = remote("GET", f"/api/editor/judge/runs/{run['id']}/testcases/{index}/output", user, timeout=15)
                    content = base64.b64decode(output["base64"], validate=True)
                    if len(content) != record["output_size"] or hashlib.sha256(content).hexdigest() != record["output_sha256"]:
                        raise ValueError("Output checksum mismatch")
                    cache[key] = pipeline.blob(content)
                except (HTTPException, ValueError, KeyError):
                    output_error = "실제 출력 파일을 가져오지 못했습니다. 원본 채점 기록은 유지됩니다."
            actual_sha = cache.get(key)
        result.append({"testset": set_name, "index": index, "verdict": VERDICTS[record["final_status"]],
                       "cpu_ms": record.get("runtime_ms"), "wall_ms": None, "memory_kb": record.get("memory_kb"),
                       "comment": record.get("judge_message", ""), "group": test.get("group", ""),
                       "input_sha": test["input_sha"], "answer_sha": test["answer_sha"], "actual_sha": actual_sha,
                       "output_truncated": record.get("output_truncated", False), "output_size": record.get("output_size"),
                       "output_total_bytes": record.get("output_total_bytes"), "output_error": output_error})
    return sorted(result, key=lambda case: case["index"])


def verify_solution(pipeline, user, name, cancellation):
    from .expectations import expected_tag, evaluate
    source = pipeline.doc["solutions"][name]
    if source["tag"] == "NR":
        return {"name": name, "tag": "NR", "matched": True, "cases": [], "skipped": True, "judge_runs": []}
    cases, records = [], []
    complete = True
    sets = selected_testsets(pipeline.doc, pipeline.job.options, pipeline.job.kind)
    for set_name, tests in pipeline.result["testsets"].items():
        if set_name not in sets:
            continue
        remaining = [t for t in tests if (pipeline.job.kind == "package" or not pipeline.job.options["test_indices"] or t["index"] in pipeline.job.options["test_indices"]) and expected_tag(source, set_name, t.get("group", ""), pipeline.doc["testsets"])[0] != "NR"]
        if remaining:
            record = run_batch(pipeline, user, name, set_name, remaining, 0, cancellation)
            records.append({key: value for key, value in record.items() if key not in {"cases", "testcase_results"}})
            verdict = VERDICTS[record["status"]]
            observed = record.get("cases", [])
            set_complete = bool(record.get("full_suite_complete")) and {case["index"] for case in observed} == {test["index"] for test in remaining}
            complete = complete and set_complete
            cases.extend(observed)
            if not set_complete and verdict in {"CE", "JUDGE_ERROR"}:
                cases.append({"testset": set_name, "index": 0, "verdict": verdict,
                              "cpu_ms": None, "wall_ms": None, "memory_kb": None,
                              "comment": record.get("compile_message") or record.get("judge_message") or "",
                              "input_sha": "", "answer_sha": "", "actual_sha": None})
            elif not set_complete:
                raise SandboxError("ZOJ 채점기가 전체 테스트의 개별 결과를 반환하지 않았습니다. 전체 검증 지원 채점기와 서버 버전을 확인하세요.")
            if getattr(pipeline, "verification", None):
                pipeline.verification.batch(name, set_name, observed, records[-1])
    verdicts = [c["verdict"] for c in cases]
    expectations = evaluate(pipeline.doc, source, pipeline.result["testsets"], cases)
    return {"name": name, "tag": source["tag"], **expectations, "matched": complete and expectations["matched"],
            "cases": sorted(cases, key=lambda c: (c["testset"], c["index"])), "skipped": False,
            "complete": complete, "judge_runs": records}


def verify(pipeline):
    compatible(pipeline.doc, pipeline.job.options, pipeline.job.kind)
    selected = list(dict.fromkeys(list(pipeline.doc["solutions"]) if pipeline.job.kind == "package" else pipeline.job.options["solutions"] or list(pipeline.doc["solutions"])))
    with SessionLocal() as db:
        account = db.get(User, pipeline.job.user_id)
        if not account:
            raise SandboxError("검증 요청 계정을 찾을 수 없습니다.")
        user = SimpleNamespace(email=account.email)
    cancellation = threading.Event()
    pipeline.judge_outputs = {}
    def limited(name):
        if getattr(pipeline, "verification", None):
            pipeline.verification.started(name)
        with execution_budget(get_settings().execution_concurrency, pipeline.sandbox.check_cancelled):
            return verify_solution(pipeline, user, name, cancellation)
    with ThreadPoolExecutor(max_workers=get_settings().judge_concurrency) as pool:
        futures = {pool.submit(limited, name): name for name in selected}
        try:
            for future in as_completed(futures):
                result = future.result()
                with pipeline.result_lock:
                    pipeline.verification.finish_solution(result)
        except Exception:
            cancellation.set()
            for future in futures:
                future.cancel()
            raise
    mismatches = [s["name"] for s in pipeline.result["solutions"] if not s["matched"]]
    pipeline.result["verified"] = not mismatches and all(s.get("expectations_complete", True) for s in pipeline.result["solutions"])
    pipeline.result["mismatches"] = mismatches
    pipeline.verification.finish()
    pipeline.event("ZOJ 채점기 검증 완료" if not mismatches else "ZOJ 판정이 기대와 다르거나 전체 검증을 완료하지 못한 풀이가 있습니다.")
