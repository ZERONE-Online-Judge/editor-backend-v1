"""Editor keeps orchestration and storage; all untrusted execution is queued."""
import hashlib
import json
import time
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException
from .execution_protocol import FORMAT, pack, validate_result
from .sandbox import SandboxError
from .zoj import remote


class AgentTransport:
    def __init__(self, sandbox, context):
        self.sandbox, self.context = sandbox, context
        self.user = SimpleNamespace(email=context["email"])
        self.records = []
        self.on_result = lambda record: None

    def execute(self, config, files, runtime):
        sandbox = self.sandbox
        value = {"format": FORMAT, "operation_id": uuid4().hex, "job_kind": self.context["job_kind"],
                 "runtime": runtime, "config": config, "files": files}
        content = pack(value)
        path = sandbox.root / (value["operation_id"] + ".task.zip")
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        task_id, done, attempted = None, False, False
        try:
            while True:
                sandbox.check_cancelled()
                try:
                    attempted = True
                    task = remote("POST", "/api/editor/judge/tasks", self.user, path, digest)
                    break
                except HTTPException as error:
                    if error.status_code not in {429, 502, 503, 504}:
                        raise
                    time.sleep(1)
            task_id = task["id"]
            while task["status"] in {"waiting", "running"}:
                sandbox.check_cancelled()
                try:
                    task = remote("GET", "/api/editor/judge/tasks/" + task_id + "?wait_seconds=2", self.user, timeout=15)
                except HTTPException as error:
                    if error.status_code not in {502, 503, 504}:
                        raise
                    time.sleep(1)
            sandbox.check_cancelled()
            if task["status"] != "completed" or not isinstance(task.get("result"), dict):
                raise SandboxError(task.get("error") or "채점 에이전트가 실행을 완료하지 못했습니다.")
            result = validate_result(task["result"], config)
            done = True
            record = {"task_id": task_id, "runtime": runtime, "node_name": task.get("node_name"),
                      "agent_version": task.get("agent_version"), "execution_revision": task.get("execution_revision"),
                      "status": result["status"], "cpu_ms": result.get("cpu_ms"), "wall_ms": result.get("wall_ms"), "memory_kb": result.get("memory_kb")}
            self.records.append(record)
            self.records = self.records[-2000:]
            self.on_result(record)
            return result
        finally:
            path.unlink(missing_ok=True)
            if attempted and not done:
                try:
                    endpoint = "/api/editor/judge/tasks/" + task_id if task_id else "/api/editor/judge/task-operations/" + value["operation_id"]
                    remote("DELETE", endpoint, self.user, timeout=15)
                except Exception:
                    pass
