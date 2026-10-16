import hashlib
import time

import pytest

from app.db import Job, Problem, SessionLocal
from app.sandbox import SandboxError
from app.storage import put_blob
from test_stress_invocations import stress_pipeline


def recorded_pipeline(monkeypatch, parallelism=1, **options):
    real_time = time.monotonic
    p, calls, clock = stress_pipeline(monkeypatch, **options)
    if parallelism > 1:
        monkeypatch.setattr(time, "monotonic", real_time)
    p.parallelism = parallelism
    blobs = {}
    def blob(value):
        sha = hashlib.sha256(value).hexdigest()
        blobs[sha] = value
        return sha
    p.blob = blob
    return p, blobs


@pytest.mark.parametrize("parallelism", [1, 3])
def test_every_executed_case_keeps_passing_and_failed_io_and_its_own_metrics(monkeypatch, parallelism):
    p, blobs = recorded_pipeline(monkeypatch, parallelism, candidates=["wrong", "slow"],
        iterations=5, counterexample_limit=10, counterexample_verdicts=["TLE"])
    def run(name, content, answer=b""):
        seed = int(content.split()[-1])
        verdict = ["AC", "AC", "TLE", "MLE", "RE"][seed - 1] if name == "wrong" else "AC"
        output = b"correct\n" if name in {"main", "slow"} or seed == 1 else b"wrong\n"
        return verdict, {"output": output, "stderr": "runtime", "cpu_ms": 100 + seed if name == "main" else seed * 5,
            "wall_ms": 1000 + seed, "memory_kb": 300 if name == "wrong" and seed == 2 else 10}
    p.run_solution = run
    p.stress()
    rows = {row["name"]: row for row in p.result["stress_results"]}
    assert [case["verdict"] for case in rows["wrong"]["cases"]] == ["AC", "WA", "TLE", "MLE", "RE"]
    assert all(len(row["cases"]) == 5 for row in rows.values())
    assert rows["main"]["role"] == "reference" and rows["slow"]["role"] == "candidate"
    for name, row in rows.items():
        for index, case in enumerate(row["cases"], 1):
            assert (case["index"], case["iteration"], case["seed"]) == (index, index, index)
            assert case["command"] == f"gen {index}"
            assert case["wall_ms"] == 1000 + index
            assert blobs[case["input_sha"]] == f"gen {index}\n".encode()
            assert blobs[case["answer_sha"]] == b"correct\n"
            assert blobs[case["actual_sha"]] == (b"wrong\n" if name == "wrong" and index > 1 else b"correct\n")
    assert (rows["main"]["max_cpu_ms"], rows["main"]["max_memory_kb"]) == (105, 10)
    assert (rows["wrong"]["max_cpu_ms"], rows["wrong"]["max_memory_kb"]) == (25, 300)
    assert p.result["stress_result_summary"] == {"executions": 15, "testcase_count": 5, "max_cpu_ms": 105, "max_memory_kb": 300}
    assert len(p.result["counterexamples"]) == 1
    assert p.result["counterexamples"][0]["findings"][0]["verdict"] == "TLE"
    assert p.result["stress"]["evidence_bytes"] == sum(len(value) for value in blobs.values())


@pytest.mark.parametrize("parallelism", [1, 3])
def test_duplicate_inputs_have_no_fabricated_repeated_measurements(monkeypatch, parallelism):
    p, _ = recorded_pipeline(monkeypatch, parallelism, iterations=5)
    p.generator = lambda command: b"same input\n"
    p.stress()
    assert [len(row["cases"]) for row in p.result["stress_results"]] == [1, 1]
    assert p.result["stress"]["duplicates_skipped"] == 4
    assert p.result["stress_result_summary"]["executions"] == 2


@pytest.mark.parametrize("parallelism", [1, 3])
def test_history_storage_limit_keeps_a_complete_measured_prefix(monkeypatch, parallelism):
    monkeypatch.setattr("app.stress_search.MAX_STRESS_EVIDENCE_BYTES", 10)
    p, _ = recorded_pipeline(monkeypatch, parallelism)
    p.stress()
    assert p.result["stress"]["stop_reason"] == "evidence_limit"
    assert p.result["stress"]["iterations_completed"] == 1
    assert [len(row["cases"]) for row in p.result["stress_results"]] == [1, 1]
    assert len(p.result["counterexamples"]) == 1


@pytest.mark.parametrize("parallelism", [1, 3])
def test_reference_failure_keeps_real_metrics_without_inventing_an_answer(monkeypatch, parallelism):
    p, blobs = recorded_pipeline(monkeypatch, parallelism)
    p.run_solution = lambda *args: ("TLE", {"output": b"partial", "stderr": "timed out", "cpu_ms": 1000, "wall_ms": 1100, "memory_kb": 200})
    with pytest.raises(SandboxError, match="기준 풀이가 TLE"):
        p.stress()
    reference, candidate = p.result["stress_results"]
    assert not candidate["cases"] and candidate["max_cpu_ms"] is None
    case = reference["cases"][0]
    assert not case["answer_sha"] and blobs[case["actual_sha"]] == b"partial"
    assert case["cpu_ms"] == reference["max_cpu_ms"] == 1000
    assert p.result["stress"]["iterations_completed"] == 0


@pytest.mark.parametrize("parallelism", [1, 3])
def test_checker_errors_remain_errors_but_the_executed_case_is_available(monkeypatch, parallelism):
    p, _ = recorded_pipeline(monkeypatch, parallelism)
    p.checker = lambda *args: ("JUDGE_ERROR", {"stderr": "checker failed"})
    with pytest.raises(SandboxError, match="checker failed"):
        p.stress()
    row = p.result["stress_results"][1]["cases"][0]
    assert row["verdict"] == "JUDGE_ERROR" and row["comment"] == "checker failed"
    assert not p.result["counterexamples"]


def test_missing_measurements_stay_unknown_and_measured_zero_stays_zero(monkeypatch):
    p, _ = recorded_pipeline(monkeypatch, iterations=1)
    p.run_solution = lambda name, *args: ("AC", {"output": b"", "stderr": "", "cpu_ms": 0 if name == "main" else None})
    p.stress()
    reference, candidate = p.result["stress_results"]
    assert reference["max_cpu_ms"] == 0 and candidate["max_cpu_ms"] is None
    assert candidate["max_memory_kb"] is None
    assert p.result["stress_result_summary"]["max_cpu_ms"] == 0
    assert p.result["stress_result_summary"]["max_memory_kb"] is None


def test_saved_history_survives_reload_and_uses_existing_problem_access(owner, problem, clients, monkeypatch):
    p, _ = recorded_pipeline(monkeypatch, iterations=1)
    def blob(value):
        with SessionLocal() as db:
            sha = put_blob(db, problem, value)["sha256"]
            db.commit()
            return sha
    p.blob = blob
    p.stress()
    with SessionLocal() as db:
        row = Job(problem_id=problem, user_id=db.get(Problem, problem).owner_id, kind="stress", status="completed",
            document=p.doc, document_hash="snapshot", workspace_version=0, revision=0, options=p.job.options, result=p.result)
        db.add(row); db.commit(); job_id = row.id
    url = f"/api/problems/{problem}/jobs/{job_id}"
    reader = clients("history-reader")
    assert reader.get(url).status_code == 404
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "history-reader", "role": "READ"})
    assert reader.get(url).json()["result"]["stress_results"] == p.result["stress_results"]
    case = reader.get(url).json()["result"]["stress_results"][1]["cases"][0]
    assert reader.get(f"/api/problems/{problem}/blobs/{case['actual_sha']}").content == b"0"
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "history-reader", "role": "NONE"})
    assert reader.get(url).status_code == reader.get(f"/api/problems/{problem}/blobs/{case['input_sha']}").status_code == 404


def test_new_requests_do_not_reuse_legacy_results_that_discarded_passing_output(owner, problem):
    from test_stress_discovery import ready
    state = ready(owner, problem)
    payload = {"kind": "stress", "version": state["version"], "options": {"generator": "gen {seed}", "candidate": "wrong.py"}}
    url = f"/api/problems/{problem}/jobs"
    legacy = owner.post(url, json=payload).json()
    with SessionLocal() as db:
        row = db.get(Job, legacy["id"])
        options = dict(row.options); options.pop("stress_results_version")
        row.options, row.status, row.result = options, "completed", {"counterexamples": []}
        db.commit()
    fresh = owner.post(url, json=payload).json()
    assert fresh["id"] != legacy["id"] and fresh["options"]["stress_results_version"] == 1
    assert owner.post(url, json=payload).json()["id"] == fresh["id"]


@pytest.mark.parametrize("same_lease", [True, False])
def test_cancelled_worker_keeps_final_history_without_overwriting_another_lease(owner, problem, monkeypatch, same_lease):
    from app import worker
    from test_stress_discovery import ready
    state = ready(owner, problem)
    requested = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "stress", "version": state["version"],
        "options": {"generator": "gen {seed}", "candidate": "wrong.py"}}).json()
    job = worker.claim_job()
    final = {"stress_results": [{"name": "wrong.py", "max_cpu_ms": 7, "cases": [{"index": 1, "verdict": "WA"}]}]}
    class CancelledPipeline:
        def __init__(self, *args):
            self.result = final
        def run(self):
            with SessionLocal() as db:
                row = db.get(Job, job.id)
                row.status = "cancelled"
                row.result = {"summary": "cancellation requested"}
                if not same_lease:
                    row.lease = "another-worker"
                db.commit()
            raise worker.Cancelled("cancelled")
    monkeypatch.setattr(worker, "Pipeline", CancelledPipeline)
    worker.process_job(job)
    saved = owner.get(f"/api/problems/{problem}/jobs/{requested['id']}").json()
    assert saved["status"] == "cancelled"
    if same_lease:
        assert saved["result"]["stress_results"] == final["stress_results"]
    else:
        assert saved["result"] == {"summary": "cancellation requested"}
