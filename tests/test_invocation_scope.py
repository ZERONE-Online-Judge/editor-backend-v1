import copy
from types import SimpleNamespace
import pytest
from app.pipeline import Pipeline
from app.schema import empty_document
from app.storage import put_blob
from app.db import SessionLocal


def scoped_pipeline(problem, kind="prepare", **options):
    doc = empty_document("Scoped generation")
    doc["testsets"] = {}
    with SessionLocal() as db:
        for name in ("tests", "samples", "subtask1"):
            sha = put_blob(db, problem, (name + "\n").encode())["sha256"]
            doc["testsets"][name] = {"script": "", "groups_enabled": False, "groups": {}, "tests": {"1": {
                "index": 1, "input_sha": sha, "answer_sha": None, "command": "",
                "group": "", "points": 0, "description": "", "sample": False,
            }}}
        db.commit()
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.doc = doc
    pipeline.job = SimpleNamespace(kind=kind, options={"testset": "tests", **options})
    pipeline.result = {"testsets": {}}
    pipeline.main = lambda: "main.cpp"
    pipeline.sandbox = SimpleNamespace(check_cancelled=lambda: None)
    pipeline.event = lambda *args, **kwargs: None
    pipeline.blob = lambda content: "generated-sha"
    pipeline.checker = lambda *args: ("AC", {})
    pipeline.run_solution = lambda *args, **kwargs: ("AC", {"output": b"42\n", "cpu_ms": 1, "wall_ms": 1, "memory_kb": 1024})
    seen = []
    pipeline.validator = lambda content, testset, group: (seen.append(testset) is None, {})
    return pipeline, seen


def test_selected_set_does_not_execute_other_scripts(problem):
    pipeline, seen = scoped_pipeline(problem, testset="samples")
    pipeline.doc["testsets"]["tests"]["script"] = "invalid script should not run"
    before = copy.deepcopy(pipeline.doc)
    pipeline.prepare()
    assert seen == ["samples"]
    assert list(pipeline.result["testsets"]) == ["samples"]
    assert pipeline.result["processed_testsets"] == ["samples"]
    assert pipeline.doc == before


@pytest.mark.parametrize("kind,options", [("prepare", {"all_testsets": True}), ("verify", {"all_testsets": True}), ("package", {})])
def test_explicit_all_sets_and_package_keep_all_sets(problem, kind, options):
    pipeline, seen = scoped_pipeline(problem, kind, **options)
    pipeline.prepare()
    assert seen == ["tests", "samples", "subtask1"]
    assert list(pipeline.result["testsets"]) == seen


def test_verification_does_not_prepare_tests_outside_selected_indices(problem):
    pipeline, seen = scoped_pipeline(problem, "verify", test_indices=[2])
    testset = pipeline.doc["testsets"]["tests"]
    testset["tests"]["2"] = {**testset["tests"]["1"], "index": 2}
    pipeline.prepare()
    assert seen == ["tests"]
    assert [t["index"] for t in pipeline.result["testsets"]["tests"]] == [2]


def test_all_sets_flag_cannot_expand_component_test_scope(owner, problem):
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "validator-tests", "version": 0, "options": {"all_testsets": True},
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_options"


def test_committed_verification_does_not_regenerate_or_replace_answers(problem):
    pipeline, _ = scoped_pipeline(problem, "verify", snapshot="committed")
    with SessionLocal() as db:
        sha = put_blob(db, problem, b"committed answer\n")["sha256"]
        db.commit()
    test = pipeline.doc["testsets"]["tests"]["tests"]["1"]
    test["answer_sha"], test["command"] = sha, "must-not-regenerate"
    pipeline.generator = lambda *args: pytest.fail("committed input must not regenerate")
    pipeline.run_solution = lambda *args, **kwargs: pytest.fail("committed answer must not replace")
    pipeline.blob = lambda *args: pytest.fail("committed test resources must stay immutable")
    pipeline.prepare()
    assert pipeline.result["testsets"]["tests"][0]["answer_sha"] == sha


def test_partial_scope_cannot_claim_full_score_or_unexecuted_extra_tags(problem):
    pipeline, _ = scoped_pipeline(problem, "verify", test_indices=[2], solutions=["main.cpp"])
    pipeline.doc["info"]["points_enabled"] = True
    pipeline.doc["solutions"] = {"main.cpp": {"tag": "MA", "extra_tags": {"samples": "WA"}}}
    tests = pipeline.doc["testsets"]["tests"]["tests"]
    tests["2"] = {**tests["1"], "index": 2, "points": 10}
    pipeline.remote_judging = False
    pipeline.result["solutions"] = []
    pipeline.publish = lambda *args: None
    def store(content):
        with SessionLocal() as db:
            value = put_blob(db, problem, content)["sha256"]
            db.commit()
            return value
    pipeline.blob = store
    pipeline.run_solution = lambda *args, **kwargs: ("AC", {"output": b"42\n", "cpu_ms": 1, "wall_ms": 1, "memory_kb": 1, "stderr": ""})
    pipeline.checker = lambda *args: ("AC", {"score": 10, "stderr": ""})
    pipeline.verify()
    solution = pipeline.result["solutions"][0]
    assert solution["matched"] is True
    assert solution["total_score"] is None and solution["score_scope_complete"] is False
    assert [row["index"] for row in solution["cases"]] == [2]
