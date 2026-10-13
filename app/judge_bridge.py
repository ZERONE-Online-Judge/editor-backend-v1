"""Final solution verification and preparation on authorized judge agents."""
import copy
import hashlib
import json
import tempfile
import time
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from .auth import current_user
from .config import get_settings
from .db import SessionLocal, User
from .domain import fail
from .sandbox import SandboxError, VENDOR
from .storage import read_blob
from .zoj import remote

router = APIRouter(prefix="/api", tags=["judge environment"])
LANGUAGES = {"cpp17": "cpp17", "python3": "python313", "c99": "c99", "java8": "java8"}
VERDICTS = {"accepted": "AC", "wrong_answer": "WA", "compile_error": "CE", "runtime_error": "RE",
            "time_limit_exceeded": "TLE", "memory_limit_exceeded": "MLE", "output_limit_exceeded": "OLE",
            "system_error": "JUDGE_ERROR", "cancelled": "JUDGE_ERROR"}
ACTIVE = {"waiting", "preparing", "judging"}


def capabilities(user):
    result = remote("GET", "/api/editor/judge/capabilities", user, timeout=15)
    if result.get("backend") != "zoj" or not isinstance(result.get("runtime_revision"), str):
        fail("judge_protocol_invalid", "ZOJ 채점 연결의 응답 형식이 올바르지 않습니다.", 502)
    return result


@router.get("/execution-environment")
def environment(user: User = Depends(current_user)):
    settings = get_settings()
    result = {"default_backend": settings.judge_backend, "zoj_configured": bool(settings.zoj_api_url), "zoj": None, "execution_transport": settings.execution_transport}
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
    if info["points_enabled"] or any(t["groups_enabled"] for t in doc["testsets"].values()):
        issues.append("그룹·부분 점수는 Editor 격리 실행을 선택해 검증하세요.")
    if not 100 <= info["time_limit_ms"] <= 10000 or not 16 <= info["memory_limit_mb"] <= 1024:
        issues.append("ZOJ 문제 제한 범위는 100~10,000ms, 16~1,024MiB입니다.")
    if any(f.get("compile_with_solution") for f in doc["files"].values()):
        issues.append("grader와 결합하는 풀이는 Editor 격리 실행으로 검증하세요.")
    selected = list(doc["solutions"]) if kind == "package" else options["solutions"] or list(doc["solutions"])
    for name in selected:
        s = doc["solutions"][name]
        if s["tag"] == "NR":
            continue
        if s["language"] not in LANGUAGES:
            issues.append(f"{name}: ZOJ 지원 언어는 C++17·C99·Python 3.13·Java 8입니다. 언어 표준을 자동으로 바꾸지 않습니다.")
        if s["tag"] == "PE" or s.get("extra_tags"):
            issues.append(f"{name}: PE·테스트셋별 추가 태그는 Editor 격리 실행을 선택해 검증하세요.")
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
        if f["kind"] in {"resource", "aux"} and filename not in resource_names:
            resource(filename, read_blob(f["sha256"]), "package-resource")
    cases = []
    for t in tests:
        for sha in (t["input_sha"], t["answer_sha"]):
            blobs[sha] = read_blob(sha)
        cases.append({"index": t["index"], "input_sha": t["input_sha"], "answer_sha": t["answer_sha"]})
    operation = hashlib.sha256(json.dumps([pipeline.job.id, name, set_name, [t["index"] for t in tests], attempt], separators=(",", ":")).encode()).hexdigest()[:32]
    manifest = {"format": "zoj-editor-judge-v1", "operation_id": operation,
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


def run_batch(pipeline, user, name, set_name, tests, attempt, cancellation, previous=None):
    path, digest = (None, None) if previous else archive(pipeline, name, set_name, tests, attempt)
    endpoint = "/api/editor/judge/runs"
    if previous:
        operation = hashlib.sha256(json.dumps([pipeline.job.id, name, set_name, [t["index"] for t in tests], attempt], separators=(",", ":")).encode()).hexdigest()[:32]
        endpoint += "/" + previous["id"] + "/remaining?" + urlencode({"operation_id": operation})
    run_id = None
    done = False
    try:
        for retry in range(3):
            pipeline.sandbox.check_cancelled()
            if cancellation.is_set():
                raise SandboxError("다른 풀이 검증이 실패하여 함께 실행하던 작업을 중단했습니다.")
            try:
                run = remote("POST", endpoint, user, path, digest)
                break
            except HTTPException as e:
                if retry == 2 or e.status_code not in {429, 502, 503, 504}:
                    raise
                time.sleep(1)
        run_id = run["id"]
        last_progress = None
        while run["status"] in ACTIVE:
            pipeline.sandbox.check_cancelled()
            if cancellation.is_set():
                raise SandboxError("다른 풀이 검증이 실패하여 함께 실행하던 작업을 중단했습니다.")
            progress = (run["status"], run["progress_current"], run["progress_total"])
            if progress != last_progress:
                pipeline.event(f"ZOJ 채점 · {name} / {set_name} · {run['progress_current']}/{run['progress_total']}", node=run.get("node_name"), judge_run_id=run_id)
                last_progress = progress
            time.sleep(1)
            run = remote("GET", "/api/editor/judge/runs/" + run_id, user, timeout=15)
        pipeline.sandbox.check_cancelled()
        if run["status"] not in VERDICTS or not run.get("result"):
            raise SandboxError("ZOJ 채점기가 최종 판정을 반환하지 않았습니다.")
        done = True
        return {"id": run_id, "testset": set_name, "status": run["status"], "node_name": run.get("node_name"),
                "agent_version": run.get("agent_version"), "runtime_revision": run["runtime_revision"],
                "testcase_count": len(tests), **run["result"]}
    finally:
        if path:
            path.unlink(missing_ok=True)
        if run_id and not done:
            try:
                remote("DELETE", "/api/editor/judge/runs/" + run_id, user, timeout=15)
            except Exception:
                pass


def verify_solution(pipeline, user, name, cancellation):
    source = pipeline.doc["solutions"][name]
    if source["tag"] == "NR":
        return {"name": name, "tag": "NR", "matched": True, "cases": [], "skipped": True, "judge_runs": []}
    cases, records = [], []
    complete = True
    for set_name, tests in pipeline.result["testsets"].items():
        if pipeline.job.kind != "package" and set_name != pipeline.job.options["testset"]:
            continue
        remaining = [t for t in tests if pipeline.job.kind == "package" or not pipeline.job.options["test_indices"] or t["index"] in pipeline.job.options["test_indices"]]
        attempt = 0
        previous = None
        while remaining:
            record = run_batch(pipeline, user, name, set_name, remaining, attempt, cancellation, previous)
            records.append(record)
            verdict = VERDICTS[record["status"]]
            failed = record.get("failed_testcase_order")
            observed = remaining if verdict == "AC" else [t for t in remaining if t["index"] == failed]
            if verdict != "AC" and not observed:
                complete = False
                # Compile/environment failures have no individual testcase.
                cases.append({"testset": set_name, "index": 0, "verdict": verdict,
                              "cpu_ms": None, "wall_ms": None, "memory_kb": None,
                              "comment": record.get("compile_message") or record.get("judge_message") or "",
                              "input_sha": "", "answer_sha": "", "actual_sha": None})
                break
            for test in observed:
                cases.append({"testset": set_name, "index": test["index"], "verdict": verdict,
                              # A batch maximum is not a per-test measurement.
                              "cpu_ms": None, "wall_ms": None, "memory_kb": record.get("memory_kb") if len(remaining) == 1 else None,
                              "comment": record.get("judge_message") or "",
                              "input_sha": test["input_sha"], "answer_sha": test["answer_sha"], "actual_sha": None})
            if verdict == "AC":
                break
            remaining = [t for t in remaining if t["index"] != failed]
            attempt += 1
            previous = record
            # The agent stops at its first failure. Re-run the other tests so
            # Polygon tags such as TO/TM retain their full-suite meaning.
            if source["tag"] in {"MA", "OK"} or verdict in {"CE", "JUDGE_ERROR"}:
                complete = not remaining
                break
    verdicts = [c["verdict"] for c in cases]
    return {"name": name, "tag": source["tag"], "matched": complete and pipeline.expected_matches(source["tag"], verdicts),
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
    with ThreadPoolExecutor(max_workers=get_settings().judge_concurrency) as pool:
        futures = {pool.submit(verify_solution, pipeline, user, name, cancellation): name for name in selected}
        try:
            for future in as_completed(futures):
                result = future.result()
                with pipeline.result_lock:
                    pipeline.result["solutions"].append(result)
                    pipeline.publish(copy.deepcopy(pipeline.result))
        except Exception:
            cancellation.set()
            for future in futures:
                future.cancel()
            raise
    mismatches = [s["name"] for s in pipeline.result["solutions"] if not s["matched"]]
    pipeline.result["verified"] = not mismatches
    pipeline.result["mismatches"] = mismatches
    pipeline.event("ZOJ 채점기 검증 완료" if not mismatches else "ZOJ 판정이 기대와 다르거나 전체 검증을 완료하지 못한 풀이가 있습니다.")
