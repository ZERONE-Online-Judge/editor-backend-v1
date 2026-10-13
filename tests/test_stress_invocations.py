import copy
import hashlib
from types import SimpleNamespace
import threading
import pytest
from app.pipeline import Pipeline
from app.db import Job, Problem, SessionLocal
from app.domain import document_hash
from app.storage import put_blob


def stress_pipeline(monkeypatch, **options):
    clock = {"now": 0.0}
    monkeypatch.setattr("app.pipeline.time.monotonic", lambda: clock["now"])
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.doc = {"solutions": {"main": {"tag": "MA"}, "wrong": {"tag": "WA"}, "slow": {"tag": "TL"}}}
    pipeline.job = SimpleNamespace(options={"reference": "main", "candidate": "wrong", "generator": "gen {seed}", "testset": "tests", "iterations": 3, "stop_on_first": False, "counterexample_limit": 5, **options})
    pipeline.result = {"events": []}
    pipeline.last_publish = 0
    pipeline.result_lock = threading.RLock()
    pipeline.publish = lambda value: None
    pipeline.sandbox = SimpleNamespace(check_cancelled=lambda: None)
    pipeline.main = lambda: "main"
    pipeline.blob = lambda value: hashlib.sha256(value).hexdigest()
    calls = {"generator": [], "validator": [], "main": [], "wrong": [], "slow": []}
    def generator(command):
        calls["generator"].append(command)
        clock["now"] += .75
        pipeline.sandbox.check_cancelled()
        return (command + "\n").encode()
    def validator(content, testset):
        calls["validator"].append(content)
        return True, {}
    def run(name, inp, answer=b""):
        calls[name].append(inp)
        clock["now"] += .1
        pipeline.sandbox.check_cancelled()
        return ("TLE" if name == "slow" else "AC"), {"output": b"42" if name == "main" else b"0", "stderr": "", "cpu_ms": 1, "memory_kb": 2}
    pipeline.generator, pipeline.validator, pipeline.run_solution = generator, validator, run
    pipeline.checker = lambda inp, out, ans: ("AC" if out == ans else "WA", {"stderr": ""})
    return pipeline, calls, clock


def test_shared_inputs_are_generated_once_for_multiple_solutions(monkeypatch):
    pipeline, calls, _ = stress_pipeline(monkeypatch, candidates=["wrong", "slow"])
    pipeline.stress()
    assert len(calls["generator"]) == len(calls["validator"]) == len(calls["main"]) == 3
    assert calls["wrong"] == calls["slow"] == calls["main"]
    assert len(pipeline.result["counterexamples"]) == 3
    assert [f["verdict"] for f in pipeline.result["counterexamples"][0]["findings"]] == ["WA", "TLE"]
    assert pipeline.result["stress"]["stop_reason"] == "iterations"
    assert all(row["tested"] == 3 and row["first_counterexample_id"] for row in pipeline.result["candidate_summaries"])


def test_first_counterexample_still_runs_all_candidates_on_that_input(monkeypatch):
    pipeline, calls, _ = stress_pipeline(monkeypatch, candidates=["wrong", "slow"], stop_on_first=True)
    pipeline.stress()
    assert len(calls["generator"]) == len(calls["wrong"]) == len(calls["slow"]) == 1
    assert pipeline.result["stress"]["stop_reason"] == "first_counterexample"


def test_target_count_limits_unique_counterexamples(monkeypatch):
    pipeline, _, _ = stress_pipeline(monkeypatch, iterations=1000)
    pipeline.stress()
    assert len(pipeline.result["counterexamples"]) == 5
    assert pipeline.result["iterations"] == 5
    assert pipeline.result["stress"]["stop_reason"] == "counterexample_target"


def test_repeated_input_is_not_rechecked_or_added_twice(monkeypatch):
    pipeline, calls, _ = stress_pipeline(monkeypatch, iterations=5)
    pipeline.generator = lambda command: calls["generator"].append(command) or b"same\n"
    pipeline.stress()
    assert len(calls["generator"]) == 5
    assert len(calls["validator"]) == len(calls["wrong"]) == 1
    assert pipeline.result["stress"]["duplicates_skipped"] == 4
    assert pipeline.result["iterations"] == 5
    assert len(pipeline.result["counterexamples"]) == 1


def test_duration_can_interrupt_an_active_iteration_without_claiming_tle(monkeypatch):
    pipeline, calls, clock = stress_pipeline(monkeypatch, iterations=1000, duration_seconds=2)
    original = pipeline.sandbox.check_cancelled
    pipeline.stress()
    stats = pipeline.result["stress"]
    assert stats["stop_reason"] == "duration"
    assert stats["iterations_completed"] == 2 and stats["iterations_started"] == 3
    assert stats["elapsed_seconds"] == round(clock["now"], 2)
    assert len(calls["wrong"]) == 2
    assert pipeline.sandbox.check_cancelled is original
    assert all(f["verdict"] == "WA" for ce in pipeline.result["counterexamples"] for f in ce["findings"])


def test_collection_can_filter_verdict_kinds(monkeypatch):
    pipeline, _, _ = stress_pipeline(monkeypatch, candidates=["wrong", "slow"], counterexample_verdicts=["TLE"])
    pipeline.stress()
    assert all([f["verdict"] for f in ce["findings"]] == ["TLE"] for ce in pipeline.result["counterexamples"])
    assert pipeline.result["candidate_summaries"][0]["first_counterexample_id"] is None


def counterexample_job(owner, problem):
    state = owner.get(f"/api/problems/{problem}").json()
    with SessionLocal() as db:
        examples = []
        for i in (1, 2):
            inp = put_blob(db, problem, f"{i}\n".encode())["sha256"]
            answer = put_blob(db, problem, f"{i+1}\n".encode())["sha256"]
            examples.append({"id": inp[:32], "iteration": i, "input_sha": inp, "answer_sha": answer, "findings": []})
        row = Job(problem_id=problem, user_id=db.get(Problem, problem).owner_id, kind="stress", status="completed", document=copy.deepcopy(state["document"]), document_hash=state["document_hash"], workspace_version=state["version"], revision=0,
                  options={"source_document_hash": state["document_hash"]}, result={"counterexamples": examples})
        db.add(row)
        db.commit()
        return row.id, state, examples


def test_counterexample_batch_is_atomic_idempotent_and_can_be_added_in_parts(owner, problem):
    job, state, examples = counterexample_job(owner, problem)
    path = f"/api/problems/{problem}/jobs/{job}/counterexamples/apply"
    first = owner.post(path, json={"version": state["version"], "testset": "tests", "counterexample_ids": [examples[0]["id"]]})
    assert first.status_code == 200, first.text
    assert first.json()["indices"] == [1]
    second = owner.post(path, json={"version": first.json()["version"], "testset": "tests", "counterexample_ids": [ce["id"] for ce in examples]})
    assert second.status_code == 200, second.text
    assert second.json()["added_count"] == second.json()["duplicate_count"] == 1
    assert second.json()["indices"] == [2]
    third = owner.post(path, json={"version": second.json()["version"], "testset": "tests", "counterexample_ids": [ce["id"] for ce in examples]})
    assert third.json()["added_count"] == 0 and third.json()["duplicate_count"] == 2
    assert len(owner.get(f"/api/problems/{problem}").json()["document"]["testsets"]["tests"]["tests"]) == 2


def test_counterexample_import_protects_newer_edits_and_reviewer_permissions(owner, problem, clients):
    job, state, examples = counterexample_job(owner, problem)
    path = f"/api/problems/{problem}/jobs/{job}/counterexamples/apply"
    reader = clients("reviewer")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "reviewer", "role": "READ", "reviewer": True})
    payload = {"version": state["version"], "testset": "tests", "counterexample_ids": [examples[0]["id"]]}
    assert reader.post(path, json=payload).status_code == 403
    state["document"]["info"]["name"] = "newer edit"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert owner.post(path, json=payload).json()["error"]["code"] == "workspace_changed"
    payload["version"] = saved.json()["version"]
    assert owner.post(path, json=payload).json()["error"]["code"] == "stale_results"
    assert not owner.get(f"/api/problems/{problem}").json()["document"]["testsets"]["tests"]["tests"]


def test_counterexample_import_reserves_generator_script_indices(owner, problem):
    job, state, examples = counterexample_job(owner, problem)
    with SessionLocal() as db:
        row = db.get(Job, job)
        row.document["testsets"]["tests"]["script"] = "gen 1 > 1"
        row.document_hash = document_hash(row.document)
        row.options = {"source_document_hash": row.document_hash}
        from app.db import WorkingCopy
        wc = db.get(WorkingCopy, (problem, row.user_id))
        wc.document = copy.deepcopy(row.document)
        db.commit()
    response = owner.post(f"/api/problems/{problem}/jobs/{job}/counterexamples/apply", json={"version": state["version"], "testset": "tests", "counterexample_ids": [examples[0]["id"]]})
    assert response.status_code == 200, response.text
    assert response.json()["indices"] == [2]
