import copy
import threading
from types import SimpleNamespace

import pytest

from app import judge_bridge as bridge
from app.db import Job, Problem, Revision, SessionLocal
from app.storage import put_blob
from app.verification_progress import VerificationProgress
from test_invocation_options import ready
from test_invocation_scope import scoped_pipeline
from test_judge_bridge import fixture_data, record
from test_reviewer_invocations import reviewer


def multiple_sets(owner, problem):
    state = ready(owner, problem)
    doc = state["document"]
    doc["testsets"]["samples"] = copy.deepcopy(doc["testsets"]["tests"])
    doc["testsets"]["empty"] = {"tests": {}, "script": "", "groups": {}, "groups_enabled": False}
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": doc})
    assert saved.status_code == 200, saved.text
    return saved.json(), doc


def test_all_sets_scope_is_explicit_saved_and_distinct_from_single_set(owner, problem):
    saved, doc = multiple_sets(owner, problem)
    options = {"all_testsets": True, "solutions": ["main.cpp"]}
    request = {"kind": "verify", "version": saved["version"], "options": options}
    response = owner.post(f"/api/problems/{problem}/jobs", json=request)
    assert response.status_code == 202, response.text
    scope = response.json()["options"]["scope"]
    assert scope == {"testset": "tests", "all_testsets": True, "testsets": ["tests", "samples", "empty"],
                     "testset_counts": {"tests": 3, "samples": 3, "empty": 0}, "test_count": 6,
                     "solutions": ["main.cpp"], "test_indices": []}
    repeated = owner.post(f"/api/problems/{problem}/jobs", json=request)
    assert repeated.json()["id"] == response.json()["id"]
    options["all_testsets"] = False
    single = owner.post(f"/api/problems/{problem}/jobs", json=request)
    assert single.status_code == 202 and single.json()["id"] != response.json()["id"]
    with SessionLocal() as db:
        assert db.get(Job, response.json()["id"]).document == doc


def test_all_sets_accepts_custom_set_names_without_requiring_tests(owner, problem):
    saved, doc = multiple_sets(owner, problem)
    doc["testsets"] = {"custom": doc["testsets"]["tests"]}
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": saved["version"], "document": doc}).json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": saved["version"], "options": {"all_testsets": True}})
    assert response.status_code == 202, response.text
    assert response.json()["options"]["scope"]["testsets"] == ["custom"]


@pytest.mark.parametrize("kind", ["verify", "prepare"])
def test_all_sets_rejects_ambiguous_test_numbers(owner, problem, kind):
    saved, _ = multiple_sets(owner, problem)
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": kind, "version": saved["version"], "options": {
        "all_testsets": True, "test_indices": [1],
    }})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_options"


def test_all_empty_sets_cannot_claim_verification(owner, problem):
    saved, doc = multiple_sets(owner, problem)
    doc["testsets"] = {"empty": doc["testsets"]["empty"]}
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": saved["version"], "document": doc}).json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": saved["version"], "options": {"all_testsets": True}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"empty_scope", "not_ready"}


def test_reviewer_all_sets_are_from_committed_revision_only(owner, problem, clients):
    _, doc = multiple_sets(owner, problem)
    with SessionLocal() as db:
        db.get(Problem, problem).document = copy.deepcopy(doc)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    user = reviewer(owner, clients, problem)
    response = user.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": 0, "options": {"all_testsets": True}})
    assert response.status_code == 202, response.text
    assert response.json()["options"]["snapshot"] == "committed"
    assert response.json()["options"]["scope"]["test_count"] == 6
    assert user.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": 0, "options": {
        "all_testsets": True, "snapshot": "working_copy",
    }}).status_code == 403


def test_local_all_sets_preserves_group_expectations_and_distinct_case_ids(problem):
    p, _ = scoped_pipeline(problem, "verify", all_testsets=True, solutions=["main.cpp", "small.cpp", "skip.cpp"], test_indices=[])
    p.remote_judging = False
    p.doc["solutions"] = {
        "main.cpp": {"tag": "MA"},
        "small.cpp": {"tag": "TL", "extra_tags": {"samples": "OK", "subtask1": "NR"},
                      "group_tags": {"tests": {"small": "OK", "large": "TL"}}},
        "skip.cpp": {"tag": "NR"},
    }
    def store(content):
        with SessionLocal() as db:
            sha = put_blob(db, problem, content)["sha256"]
            db.commit()
            return sha
    tests = p.doc["testsets"]["tests"]
    tests["groups_enabled"] = True
    rule = {"points_policy": "COMPLETE_GROUP", "feedback_policy": "COMPLETE", "dependencies": []}
    tests["groups"] = {name: copy.deepcopy(rule) for name in ["small", "large"]}
    tests["tests"]["1"]["group"] = "small"
    tests["tests"]["2"] = {**tests["tests"]["1"], "index": 2, "group": "large", "input_sha": store(b"large\n")}
    p.doc["testsets"]["empty"] = {"tests": {}, "script": "", "groups_enabled": False, "groups": {}}
    seen = []
    p.validator = lambda inp, name, group: (seen.append((name, group)) is None, {})
    p.blob = store
    p.run_solution = lambda name, inp, *args, **kwargs: (
        "TLE" if name == "small.cpp" and inp == b"large\n" else "AC",
        {"output": b"42\n", "cpu_ms": 5, "memory_kb": 100, "wall_ms": 8, "stderr": ""})
    p.checker = lambda *args: ("AC", {"stderr": ""})
    snapshots = []
    p.publish = lambda value: snapshots.append(copy.deepcopy(value))
    p.verify()
    assert seen == [("tests", "small"), ("tests", "large"), ("samples", ""), ("subtask1", "")]
    assert p.result["processed_testsets"] == ["tests", "samples", "subtask1", "empty"]
    assert p.result["verified"]
    small = next(s for s in p.result["solutions"] if s["name"] == "small.cpp")
    assert [(s["testset"], s["group"], s["tag"], s["skipped"]) for s in small["expectations"]] == [
        ("tests", "small", "OK", False), ("tests", "large", "TL", False),
        ("samples", "", "OK", False), ("subtask1", "", "NR", True)]
    assert {(c["testset"], c["index"]) for c in small["cases"]} == {("tests", 1), ("tests", 2), ("samples", 1)}
    progress = p.result["verification_progress"]
    assert (progress["current"], progress["total"], progress["percent"]) == (7, 7, 100)
    assert progress["testsets"] == {
        "tests": {"current": 4, "total": 4, "percent": 100},
        "samples": {"current": 2, "total": 2, "percent": 100},
        "subtask1": {"current": 1, "total": 1, "percent": 100},
        "empty": {"current": 0, "total": 0, "percent": 0}}
    assert any(s["verification_progress"]["current"] == 1 for s in snapshots)


@pytest.mark.parametrize("expected_match", [True, False])
def test_zoj_all_sets_uses_separate_batches_and_expectations(problem, tmp_path, monkeypatch, expected_match):
    p = fixture_data(problem, tmp_path, "TL")
    p.job.options["all_testsets"] = True
    p.doc["solutions"]["main.cpp"]["extra_tags"] = {"tests": "OK", "samples": "TL"}
    p.doc["testsets"]["samples"] = copy.deepcopy(p.doc["testsets"]["tests"])
    p.doc["testsets"]["samples"]["groups_enabled"] = True
    p.doc["testsets"]["samples"]["groups"] = {"small": {}, "large": {}}
    for test in p.doc["testsets"]["samples"]["tests"].values():
        test["group"] = "large" if test["index"] == 3 else "small"
    p.doc["solutions"]["main.cpp"]["group_tags"] = {"samples": {"small": "OK", "large": "TL"}}
    p.result["testsets"]["samples"] = list(p.doc["testsets"]["samples"]["tests"].values())
    p.publish = lambda *_: None
    p.verification = VerificationProgress(p, ["main.cpp"])
    p.verification.begin()
    calls = []
    original = bridge.archive
    def archive(pipeline, name, set_name, tests, attempt):
        calls.append((set_name, [t["index"] for t in tests]))
        return original(pipeline, name, set_name, tests, attempt)
    monkeypatch.setattr(bridge, "archive", archive)
    responses = iter([record(), record("time_limit_exceeded", 3) if expected_match else record()])
    monkeypatch.setattr(bridge, "remote", lambda *args, **kwargs: next(responses))
    result = bridge.verify_solution(p, SimpleNamespace(email="author@example.com"), "main.cpp", threading.Event())
    p.verification.finish_solution(result)
    p.verification.finish()
    assert calls == [("tests", [1, 2, 3]), ("samples", [1, 2, 3])]
    assert result["complete"] and result["expectations_complete"]
    assert result["matched"] is expected_match
    assert len(result["judge_runs"]) == 2 and len(result["cases"]) == 6
    assert p.result["verification_progress"]["percent"] == 100
    assert all(s["current"] == s["total"] == 3 for s in p.result["verification_progress"]["testsets"].values())
