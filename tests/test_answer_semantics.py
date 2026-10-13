import hashlib
from app.db import SessionLocal
from app.storage import put_blob
from test_invocation_scope import scoped_pipeline


def test_interactive_preparation_keeps_interactor_reference_data(problem):
    pipeline, _ = scoped_pipeline(problem)
    pipeline.doc["info"]["interactive"] = True
    with SessionLocal() as db:
        sha = put_blob(db, problem, b"interactor reference\n")["sha256"]
        db.commit()
    pipeline.doc["testsets"]["tests"]["tests"]["1"]["answer_sha"] = sha
    supplied = []
    def run(name, inp, answer, **kwargs):
        supplied.append(answer)
        return "AC", {"output": b"interactive transcript\n", "cpu_ms": 1, "wall_ms": 2, "memory_kb": 3}
    pipeline.run_solution = run
    pipeline.blob = lambda value: hashlib.sha256(value).hexdigest()
    pipeline.prepare()
    assert supplied == [b"interactor reference\n"]
    assert pipeline.result["testsets"]["tests"][0]["answer_sha"] == sha


def test_ordinary_preparation_replaces_provisional_output_with_main(problem):
    pipeline, _ = scoped_pipeline(problem)
    with SessionLocal() as db:
        sha = put_blob(db, problem, b"provisional wrong answer\n")["sha256"]
        db.commit()
    pipeline.doc["testsets"]["tests"]["tests"]["1"]["answer_sha"] = sha
    pipeline.blob = lambda value: hashlib.sha256(value).hexdigest()
    pipeline.prepare()
    assert pipeline.result["testsets"]["tests"][0]["answer_sha"] == hashlib.sha256(b"42\n").hexdigest()
