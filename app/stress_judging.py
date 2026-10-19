"""Stress verdicts use the same queue and resource policy as contest judging."""
import threading
from uuid import uuid4
from .db import SessionLocal, User
from .judge_bridge import run_batch
from .storage import read_blob
from .sandbox import SandboxError


def run(pipeline, name, content, answer):
    reference = answer is None
    if reference:
        # Answer production is preparation, not a verdict or a measurement.
        # The reference is then judged again under the real contest limits.
        pipeline.event(f"기준 정답 생성 · {name}")
        verdict, prepared = pipeline.run_solution(name, content, preparation=True)
        if verdict != "AC":
            raise SandboxError(f"기준 정답 생성이 {verdict}로 실패했습니다. ZOJ 판정 전 준비 단계의 오류입니다.")
        answer = prepared["output"]
    with SessionLocal() as db:
        user = db.get(User, pipeline.job.user_id)
        if user is None:
            raise SandboxError("실행 요청 계정을 찾을 수 없습니다.")
        from types import SimpleNamespace
        identity = SimpleNamespace(email=user.email)
    test = {"index": 1, "input_sha": pipeline.blob(content), "answer_sha": pipeline.blob(answer), "group": pipeline.job.options.get("group", "")}
    budget = pipeline.stress_judge_budget
    while not budget.acquire(timeout=.2):
        pipeline.sandbox.check_cancelled()
    try:
        record = run_batch(pipeline, identity, name, pipeline.job.options["testset"], [test], "stress-" + uuid4().hex, threading.Event())
    finally:
        budget.release()
    with pipeline.result_lock:
        runs = pipeline.result.setdefault("stress_judge_runs", [])
        runs.append({key:record[key] for key in ("id", "node_name", "agent_version", "runtime_revision", "status")})
        runs[-1]["solution"] = name
        del runs[:-2000]
    if not record["cases"] and record["status"] == "compile_error":
        return "CE", {"output":b"", "cpu_ms":None, "wall_ms":None, "memory_kb":None, "stderr":record.get("compile_message") or record.get("judge_message") or "컴파일에 실패했습니다.", "judged_by_zoj":True}
    if len(record["cases"]) != 1:
        raise SandboxError(record.get("judge_message") or "ZOJ 채점기가 스트레스 테스트의 개별 판정을 반환하지 않았습니다.")
    case = record["cases"][0]
    if case.get("output_error"):
        raise SandboxError(case["output_error"])
    actual = read_blob(case["actual_sha"]) if case.get("actual_sha") else b""
    result = {"output": answer if reference else actual, "actual_output": actual,
              "cpu_ms": case["cpu_ms"], "wall_ms": case["wall_ms"], "memory_kb": case["memory_kb"],
              "stderr": case["comment"], "judged_by_zoj": True,
              **{key:case.get(key) for key in ("output_truncated", "output_size", "output_total_bytes", "output_error")}}
    return case["verdict"], result
