"""Content-addressed successful preparation, never contestant verdicts or timing."""
import hashlib
import json
from pathlib import Path
from functools import lru_cache
from sqlalchemy import delete
from .config import get_settings
from .db import Blob, PreparationEntry, SessionLocal, now
from .resources import compile_resources
from .sandbox import VENDOR, SandboxError
from .storage import read_blob

RETENTION_SECONDS = 30 * 86400
STAGES = ("input", "validator", "answer", "checker", "sample")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def content_hash(value):
    return hashlib.sha256(value).hexdigest()


@lru_cache
def policy_digest():
    # A runner or preparation policy change must not reuse old attestations.
    root = Path(__file__).parent
    return digest({name: content_hash((root / name).read_bytes()) for name in (
        "pipeline.py", "sandbox.py", "resources.py", "statement_samples.py",
        "test_preparation.py", "preparation_cache.py", "execution_limits.py",
    )})


class PreparationCache:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.problem_id = pipeline.job.problem_id
        self.reuse = pipeline.job.kind == "verify" and not pipeline.job.options.get("force_prepare", False)
        runtime = pipeline.result.get("preparation_runtime", pipeline.result["runtime"])
        self.context = {"runtime": runtime, "policy": policy_digest(),
                        "runtime_revision": get_settings().runtime_revision,
                        "testlib": content_hash((VENDOR / "testlib.h").read_bytes())}
        self.programs = {}
        self.stats = {"enabled": self.reuse,
                      "reused": dict.fromkeys(STAGES, 0), "executed": dict.fromkeys(STAGES, 0)}
        pipeline.result["preparation_cache"] = self.stats

    def program(self, name, solution=False):
        with self.pipeline.program_lock:
            key = (name, solution)
            if key not in self.programs:
                if name.startswith("std::"):
                    source = {"name": name.removeprefix("std::") + ".cpp", "language": "cpp17",
                              "sha256": content_hash((VENDOR / (name.removeprefix("std::") + ".cpp")).read_bytes())}
                else:
                    source = self.pipeline.doc["solutions" if solution else "files"].get(name)
                    if not source:
                        raise SandboxError(f"실행 소스를 찾을 수 없습니다: {name}")
                resources = compile_resources(list(self.pipeline.doc["files"].values()), source["language"], solution)
                fields = ("name", "sha256", "language", "entry_class", "kind", "compile_with_solution", "resource_policy")
                select = lambda value: {key: value[key] for key in fields if key in value}
                self.programs[key] = {"source": select(source), "resources": [select(resource) for resource in resources]}
            return self.programs[key]

    def step(self, stage, dependencies, execute):
        pipeline = self.pipeline
        pipeline.sandbox.check_cancelled()
        key = digest({"context": self.context, "stage": stage, "dependencies": dependencies})
        value = None
        if self.reuse:
            with SessionLocal() as db:
                row = db.get(PreparationEntry, (self.problem_id, key))
                if row and row.expires_at > now() and row.stage == stage:
                    value = row.result
                    sha = value.get("blob_sha")
                    if sha:
                        try:
                            if not db.get(Blob, (self.problem_id, sha)) or content_hash(read_blob(sha)) != sha:
                                value = None
                        except (OSError, ValueError):
                            value = None
                if row and value is None:
                    db.delete(row)
                    db.commit()
        reused = value is not None
        if not reused:
            value = execute()
            pipeline.sandbox.check_cancelled()
            stamp = now()
            with SessionLocal() as db:
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
                fields = {"stage": stage, "result": value, "created_at": stamp, "expires_at": stamp + RETENTION_SECONDS}
                statement = insert(PreparationEntry).values(problem_id=self.problem_id, key=key, **fields)
                db.execute(statement.on_conflict_do_update(index_elements=["problem_id", "key"], set_=fields))
                db.commit()
        pipeline.sandbox.check_cancelled()
        with pipeline.result_lock:
            self.stats["reused" if reused else "executed"][stage] += 1
        return value, reused


def expire_entries(db):
    db.execute(delete(PreparationEntry).where(PreparationEntry.expires_at <= now()))
