"""Produce Main output with the contest judge's compiler and resource policy."""
import threading
import copy
from types import SimpleNamespace
from uuid import uuid4
from .db import SessionLocal, User
from .judge_bridge import run_batch
from .sandbox import SandboxError
from .storage import read_blob

PROTOCOL = "editor-reference-output-v1"


def run(pipeline, name, content):
    with SessionLocal() as db:
        user = db.get(User, pipeline.job.user_id)
        if user is None:
            raise SandboxError("실행 요청 계정을 찾을 수 없습니다.")
        identity = SimpleNamespace(email=user.email)
    proxy = SimpleNamespace(job=pipeline.job, doc=pipeline.doc, sandbox=pipeline.sandbox,
                            blob=pipeline.blob, event=pipeline.event, reference_output=True)
    test = {"index": 1, "input_sha": pipeline.blob(content), "answer_sha": pipeline.blob(b""), "group": ""}
    pipeline.event(f"ZOJ 채점기로 Main 정답 생성 · {name}")
    while not pipeline.stress_judge_budget.acquire(timeout=.2):
        pipeline.sandbox.check_cancelled()
    try:
        record = run_batch(proxy, identity, name, pipeline.job.options["testset"], [test],
                           "reference-" + uuid4().hex, threading.Event())
    finally:
        pipeline.stress_judge_budget.release()
    if record["status"] == "compile_error":
        return "CE", {"output": b"", "cpu_ms": None, "wall_ms": None, "memory_kb": None,
                      "stderr": record.get("compile_message") or record.get("judge_message") or "Main 컴파일에 실패했습니다."}
    if len(record["cases"]) != 1:
        raise SandboxError(record.get("judge_message") or "ZOJ 채점기가 Main 실행 결과를 반환하지 않았습니다.")
    case = record["cases"][0]
    if case.get("output_error"):
        raise SandboxError(case["output_error"])
    if case.get("output_truncated"):
        raise SandboxError("Main 출력이 보관 한도를 넘어 정답으로 저장할 수 없습니다. 일부 출력은 정답으로 반영하지 않습니다.")
    if case["verdict"] == "AC" and not case.get("actual_sha"):
        raise SandboxError("Main의 전체 출력이 확인되지 않아 정답을 저장하지 않았습니다.")
    result = {"output": read_blob(case["actual_sha"]) if case.get("actual_sha") else b"",
              "cpu_ms": case["cpu_ms"], "wall_ms": case["wall_ms"], "memory_kb": case["memory_kb"],
              "stderr": case["comment"], "judged_by_zoj": True}
    with pipeline.result_lock:
        pipeline.result.setdefault("execution_runs", []).append({"task_id": record["id"], "runtime": "reference",
            "node_name": record.get("node_name"), "agent_version": record.get("agent_version"),
            "status": {"AC":"ok", "TLE":"time-limit", "MLE":"memory-limit", "OLE":"output-limit"}.get(case["verdict"], case["verdict"]),
            "cpu_ms": case["cpu_ms"], "memory_kb": case["memory_kb"], "wall_ms": case["wall_ms"]})
        pipeline.result["execution_runs"] = pipeline.result["execution_runs"][-2000:]
        pipeline.publish(copy.deepcopy(pipeline.result))
    return case["verdict"], result
