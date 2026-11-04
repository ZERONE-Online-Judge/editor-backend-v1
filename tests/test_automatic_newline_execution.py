"""Automatic test formatting must not rewrite source evidence or verdict bytes."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import Job, Problem, Revision, SessionLocal, WorkingCopy
from app.demo import populate
from app.domain import document_hash
from app.pipeline import Pipeline
from app.storage import put_blob, read_blob
from test_invocation_scope import scoped_pipeline


def store(problem_id, content):
    with SessionLocal() as db:
        sha = put_blob(db, problem_id, content)["sha256"]
        db.commit()
        return sha


@pytest.fixture
def legacy_document(problem):
    """Represent an existing revision created before the automatic LF policy."""
    with SessionLocal() as db:
        model = db.get(Problem, problem)
        doc = populate(db, model)
        inp = put_blob(db, problem, b"1 2\r\n")["sha256"]
        ans = put_blob(db, problem, b"3\r\n")["sha256"]
        source = put_blob(db, problem, b"int main() {}\r\n")
        doc["solutions"]["main.cpp"].update(source)
        doc["testsets"]["tests"]["tests"] = {
            "1": {**doc["testsets"]["tests"]["tests"]["1"],
                  "input_sha": inp, "answer_sha": ans}}
        doc["validator_tests"] = {
            "legacy": {"input_sha": inp, "answer_sha": None,
                       "output_sha": None, "expected": "VALID"}}
        doc["checker_tests"] = {
            "legacy": {"input_sha": inp, "answer_sha": ans,
                       "output_sha": ans, "expected": "OK"}}
        model.document = copy.deepcopy(doc)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    return doc


@pytest.mark.parametrize("kind,area", [
    ("validator-tests", "validator_tests"),
    ("checker-tests", "checker_tests"),
])
def test_job_keeps_original_source_hash_but_queues_normalized_snapshot(
        owner, problem, legacy_document, kind, area):
    state = owner.get(f"/api/problems/{problem}").json()
    original = copy.deepcopy(state["document"])
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": kind, "version": state["version"], "reuse": False})
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["options"]["source_document_hash"] == document_hash(original)
    with SessionLocal() as db:
        job = db.get(Job, queued["id"])
        test = job.document[area]["legacy"]
        assert read_blob(test["input_sha"]) == b"1 2\n"
        if kind == "checker-tests":
            assert read_blob(test["answer_sha"]) == b"3\n"
            assert read_blob(test["output_sha"]) == b"3\n"
        assert job.document_hash == document_hash(job.document)
        assert job.document_hash != queued["options"]["source_document_hash"]
        assert job.document["files"] == original["files"]
        assert job.document["solutions"] == original["solutions"]
        assert db.get(Problem, problem).document == legacy_document
        assert db.get(Revision, (problem, 0)).document == legacy_document
    assert owner.get(f"/api/problems/{problem}").json()["document"] == original


def test_reviewer_verification_normalizes_snapshot_without_creating_or_editing_draft(
        owner, clients, problem, legacy_document):
    reviewer = clients("newline-reviewer")
    granted = owner.put(f"/api/problems/{problem}/accesses", json={
        "login": "newline-reviewer", "permissions": ["execute"]})
    assert granted.status_code == 200, granted.text
    before = reviewer.get(f"/api/problems/{problem}").json()
    with SessionLocal() as db:
        drafts = [(row.user_id, row.version, copy.deepcopy(row.document))
                  for row in db.scalars(select(WorkingCopy).where(
                      WorkingCopy.problem_id == problem))]
    response = reviewer.post(f"/api/problems/{problem}/jobs", json={
        "kind": "verify", "version": before["version"], "reuse": False,
        "options": {"solutions": ["main.cpp"]}})
    assert response.status_code == 202, response.text
    assert response.json()["options"]["snapshot"] == "committed"
    assert response.json()["options"]["source_document_hash"] == document_hash(legacy_document)
    with SessionLocal() as db:
        job = db.get(Job, response.json()["id"])
        test = job.document["testsets"]["tests"]["tests"]["1"]
        assert read_blob(test["input_sha"]) == b"1 2\n"
        assert read_blob(test["answer_sha"]) == b"3\n"
        assert db.get(Problem, problem).document == legacy_document
        assert db.get(Revision, (problem, 0)).document == legacy_document
        assert [(row.user_id, row.version, row.document)
                for row in db.scalars(select(WorkingCopy).where(
                    WorkingCopy.problem_id == problem))] == drafts


@pytest.mark.parametrize("content,expected", [
    (b"1 2\r\n", b"1 2\n"),
    (b"1\r2", b"1\n2"),
    (b"1 2", b"1 2"),
    (b"\xff\r\n", b"\xff\r\n"),
    (b"1\0\r\n", b"1\0\r\n"),
])
def test_real_generator_boundary_only_normalizes_text_newlines(content, expected):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.doc = {"files": {"gen.cpp": {}}}
    pipeline.program = lambda name: "compiled-generator"
    pipeline.sandbox = SimpleNamespace(run=lambda *args, **kwargs: {
        "status": "ok", "exit_code": 0, "stderr": "", "output": content})
    assert pipeline.generator("gen.cpp 1") == expected


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("input_bytes,reference_bytes,expected_input,expected_answer", [
    (b"1 2\r\n", b"42 \r\n", b"1 2\n", b"42 \n"),
    (b"1\r2", b"42", b"1\n2", b"42"),
    (b"\xff\r\n", b"\xfe\r\n", b"\xff\r\n", b"\xfe\r\n"),
    (b"1\0\r\n", b"42\0\r\n", b"1\0\r\n", b"42\0\r\n"),
])
def test_preparation_normalizes_input_and_reference_without_appending_eof_or_touching_binary(
        problem, generated, input_bytes, reference_bytes, expected_input, expected_answer):
    pipeline, _ = scoped_pipeline(problem)
    pipeline.doc["info"]["well_formed"] = False
    test = pipeline.doc["testsets"]["tests"]["tests"]["1"]
    test["input_sha"] = store(problem, input_bytes)
    if generated:
        test["command"] = "gen.cpp 1"
        pipeline.generator = lambda command: input_bytes
    seen = []
    pipeline.validator = lambda content, *args: (seen.append(("validator", content)) or True, {})
    def reference(name, content, *args, **kwargs):
        seen.append(("main", content))
        return "AC", {"output": reference_bytes, "cpu_ms": 1, "wall_ms": 2,
                      "memory_kb": 3, "stderr": ""}
    pipeline.run_solution = reference
    pipeline.checker = lambda inp, actual, answer, *args: (
        seen.append(("checker", inp, actual, answer)) or "AC", {})
    pipeline.blob = lambda content: store(problem, content)
    pipeline.prepare()
    row = pipeline.result["testsets"]["tests"][0]
    assert seen == [("validator", expected_input), ("main", expected_input),
                    ("checker", expected_input, expected_answer, expected_answer)]
    assert read_blob(row["input_sha"]) == expected_input
    assert read_blob(row["answer_sha"]) == expected_answer


def test_committed_verification_preserves_candidate_output_and_its_strict_byte_verdict(problem):
    pipeline, _ = scoped_pipeline(problem, "verify", snapshot="committed",
                                   solutions=["candidate.cpp"], test_indices=[])
    test = pipeline.doc["testsets"]["tests"]["tests"]["1"]
    test["input_sha"] = store(problem, b"1 2\r\n")
    test["answer_sha"] = store(problem, b"42\r\n")
    pipeline.doc["solutions"] = {"candidate.cpp": {"tag": "WA", "extra_tags": {}}}
    pipeline.remote_judging = False
    pipeline.publish = lambda *args: None
    pipeline.result["solutions"] = []
    pipeline.blob = lambda content: store(problem, content)
    pipeline.validator = lambda content, *args: (content == b"1 2\n", {})
    seen = []
    def execute(name, inp, answer=None, **kwargs):
        assert inp == b"1 2\n" and answer == b"42\n"
        assert not kwargs.get("preparation"), "Committed Main must not regenerate"
        return "AC", {"output": b"42\r\n", "cpu_ms": 1, "wall_ms": 1,
                      "memory_kb": 1, "stderr": ""}
    pipeline.run_solution = execute
    def checker(inp, actual, answer, *args):
        seen.append((actual, answer))
        return ("AC" if actual == answer else "WA"), {"score": 0, "stderr": ""}
    pipeline.checker = checker
    pipeline.verify()
    case = pipeline.result["solutions"][0]["cases"][0]
    assert seen == [(b"42\n", b"42\n"), (b"42\r\n", b"42\n")]
    assert case["verdict"] == "WA"
    assert read_blob(case["actual_sha"]) == b"42\r\n"
    assert read_blob(case["answer_sha"]) == b"42\n"


@pytest.mark.parametrize("kind", ["validator-tests", "checker-tests"])
@pytest.mark.parametrize("preserve", [False, True])
def test_self_test_runtime_honors_explicit_raw_fixture_bytes(problem, kind, preserve):
    pipeline, _ = scoped_pipeline(problem, kind)
    raw_input, raw_answer, raw_output = b"1 2\r\n", b"3\r\n", b"wrong\r\n"
    area = "validator_tests" if kind == "validator-tests" else "checker_tests"
    pipeline.doc[area] = {"raw": {
        "input_sha": store(problem, raw_input), "answer_sha": store(problem, raw_answer),
        "output_sha": store(problem, raw_output), "preserve_line_endings": preserve,
        "expected": "VALID" if kind == "validator-tests" else "OK"}}
    seen = []
    pipeline.result["unit_tests"] = []
    pipeline.validator = lambda content, *args: (
        seen.append((content,)) or True, {"status": "ok", "exit_code": 0, "stderr": ""})
    pipeline.checker = lambda inp, actual, answer, *args: (
        seen.append((inp, actual, answer)) or "AC", {"stderr": ""})
    pipeline.unit_tests(kind)
    values = (raw_input,) if kind == "validator-tests" else (raw_input, raw_output, raw_answer)
    expected = values if preserve else tuple(value.replace(b"\r\n", b"\n") for value in values)
    assert seen == [expected]
    assert pipeline.result["unit_tests"][0]["matched"]


def test_preparation_cache_policy_changes_when_normalization_helper_changes(monkeypatch):
    from app.preparation_cache import policy_digest
    original = Path.read_bytes
    policy_digest.cache_clear()
    baseline = policy_digest()
    try:
        with monkeypatch.context() as scoped:
            def read(path):
                value = original(path)
                return value + b"\n# alternate newline policy\n" if path.name == "test_line_endings.py" else value
            scoped.setattr(Path, "read_bytes", read)
            policy_digest.cache_clear()
            assert policy_digest() != baseline
    finally:
        policy_digest.cache_clear()


def test_queued_checker_raw_override_keeps_all_three_exact_fixture_blobs(
        owner, problem, legacy_document):
    legacy_document["checker_tests"]["legacy"]["preserve_line_endings"] = True
    with SessionLocal() as db:
        db.get(Problem, problem).document = copy.deepcopy(legacy_document)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(legacy_document)
        db.commit()
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "checker-tests", "version": state["version"], "reuse": False})
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        test = db.get(Job, response.json()["id"]).document["checker_tests"]["legacy"]
        assert test == legacy_document["checker_tests"]["legacy"]
        assert read_blob(test["input_sha"]) == b"1 2\r\n"
        assert read_blob(test["answer_sha"]) == read_blob(test["output_sha"]) == b"3\r\n"


@pytest.mark.parametrize("parallelism", [1, 3])
def test_stress_normalizes_shared_input_and_reference_but_preserves_actual_evidence(
        monkeypatch, parallelism):
    from test_stress_results import recorded_pipeline
    pipeline, blobs = recorded_pipeline(monkeypatch, parallelism, iterations=1)
    pipeline.generator = lambda command: b"1 2\r\n"
    seen = []
    def validator(content, *args):
        assert content == b"1 2\n"
        return True, {}
    pipeline.validator = validator
    def execute(name, inp, answer=None):
        assert inp == b"1 2\n"
        if name != "main":
            assert answer == b"42\n"
        return "AC", {"output": b"42\r\n", "actual_output": b"42\r\n",
                      "stderr": "", "cpu_ms": 1, "wall_ms": 2, "memory_kb": 3}
    pipeline.run_solution = execute
    def checker(inp, actual, answer):
        seen.append((actual, answer))
        return ("AC" if actual == answer else "WA"), {"stderr": ""}
    pipeline.checker = checker
    pipeline.stress()
    assert seen == [(b"42\r\n", b"42\n")]
    rows = {row["name"]: row["cases"][0] for row in pipeline.result["stress_results"]}
    assert rows["wrong"]["verdict"] == "WA"
    for case in rows.values():
        assert blobs[case["input_sha"]] == b"1 2\n"
        assert blobs[case["answer_sha"]] == b"42\n"
        assert blobs[case["actual_sha"]] == b"42\r\n"
    example = pipeline.result["counterexamples"][0]
    assert blobs[example["answer_sha"]] == b"42\n"
    assert blobs[example["findings"][0]["actual_sha"]] == b"42\r\n"


def test_native_stress_reference_answer_is_canonical_but_actual_output_stays_raw(
        problem, monkeypatch):
    import threading
    from app.stress_judging import run
    with SessionLocal() as db:
        owner_id = db.get(Problem, problem).owner_id
    actual = store(problem, b"42\r\n")
    pipeline = SimpleNamespace(
        job=SimpleNamespace(user_id=owner_id, options={"testset": "tests"}),
        event=lambda *args: None,
        run_solution=lambda *args, **kwargs: ("AC", {"output": b"42\r\n"}),
        blob=lambda content: store(problem, content),
        stress_judge_budget=threading.BoundedSemaphore(1),
        sandbox=SimpleNamespace(check_cancelled=lambda: None),
        result_lock=threading.RLock(), result={})
    seen = []
    def batch(p, identity, name, testset, tests, operation, cancelled):
        seen.extend(tests)
        return {"id": "native-run", "node_name": "node", "agent_version": "test",
                "runtime_revision": "test", "status": "completed", "cases": [{
                    "verdict": "WA", "actual_sha": actual, "cpu_ms": 1,
                    "wall_ms": 2, "memory_kb": 3, "comment": "strict bytes"}]}
    monkeypatch.setattr("app.stress_judging.run_batch", batch)
    verdict, result = run(pipeline, "main.cpp", b"1 2\n", None)
    assert read_blob(seen[0]["input_sha"]) == b"1 2\n"
    assert read_blob(seen[0]["answer_sha"]) == b"42\n"
    assert verdict == "WA"
    assert result["output"] == b"42\n"
    assert result["actual_output"] == b"42\r\n"


def test_display_sample_validation_normalizes_only_its_derived_test_data():
    from app.statement_samples import validate
    calls = []
    pipeline = SimpleNamespace(
        doc={"info": {"interactive": False}},
        validator=lambda inp, *args: (calls.append(("input", inp)) or True, {}),
        run_solution=lambda *args, **kwargs: ("AC", {"output": b"42\r\n"}),
        checker=lambda inp, actual, answer, *args: (
            calls.append(("checker", inp, actual, answer)) or "AC", {}))
    test = {"index": 1, "sample": True, "statement_input": "1 2\r\n",
            "statement_output": "42\r\n", "group": ""}
    original = copy.deepcopy(test)
    assert validate(pipeline, test, "tests", b"unused", b"unused", "main.cpp") == "VALID"
    assert calls == [("input", b"1 2\n"), ("checker", b"1 2\n", b"42\n", b"42\n")]
    assert test == original


@pytest.mark.parametrize("changed", [False, True])
def test_normalized_preparation_uses_source_identity_for_pending_and_safe_application(
        owner, problem, legacy_document, changed):
    state = owner.get(f"/api/problems/{problem}").json()
    requested = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "prepare", "version": state["version"], "reuse": False})
    assert requested.status_code == 202, requested.text
    job_id = requested.json()["id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.document_hash != job.options["source_document_hash"]
        job.status = "completed"
        job.result = {"summary": "Generated", "testsets": {"tests": [
            {**job.document["testsets"]["tests"]["tests"]["1"], "validator": "VALID"}]}}
        db.commit()
    pending = owner.get(f"/api/problems/{problem}/preparation-status").json()
    assert pending["pending_preparations"] == [
        {**pending["pending_preparations"][0], "id": job_id, "stale": False}]
    if changed:
        state["document"]["info"]["name"] = "A genuine later edit"
        saved = owner.put(f"/api/problems/{problem}/working-copy", json={
            "version": state["version"], "document": state["document"]})
        assert saved.status_code == 200, saved.text
        state["version"] = saved.json()["version"]
        assert owner.get(f"/api/problems/{problem}/preparation-status").json()[
            "pending_preparations"][0]["stale"] is True
    response = owner.post(f"/api/problems/{problem}/jobs/{job_id}/apply", json={
        "version": state["version"]})
    if changed:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "stale_results"
        assert owner.get(f"/api/problems/{problem}").json()["document"]["info"]["name"] == "A genuine later edit"
    else:
        assert response.status_code == 200, response.text
        current = owner.get(f"/api/problems/{problem}").json()
        test = current["document"]["testsets"]["tests"]["tests"]["1"]
        assert read_blob(test["input_sha"]) == b"1 2\n"
        assert read_blob(test["answer_sha"]) == b"3\n"
        assert current["pending_preparations"] == []
        assert current["document"]["validator_tests"] == legacy_document["validator_tests"]


@pytest.mark.parametrize("changed", [False, True])
def test_normalized_package_source_identity_is_shared_by_library_and_publication(
        owner, problem, legacy_document, monkeypatch, changed):
    import uuid
    from app.zoj_package import build_zoj_package
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "package", "version": state["version"], "reuse": False,
        "options": {"export_format": "zoj", "verify": True}})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.document_hash != job.options["source_document_hash"]
        artifact = build_zoj_package(job, {
            "tests": list(job.document["testsets"]["tests"]["tests"].values())})
        job.status = "completed"
        job.result = {"package": artifact}
        if changed:
            problem_row = db.get(Problem, problem)
            revised = copy.deepcopy(problem_row.document)
            revised["info"]["name"] = "A genuine new committed source"
            problem_row.document = revised
        db.commit()
    calls = []
    def remote(*args, **kwargs):
        calls.append(args)
        return {"problem_id": str(uuid.uuid4()), "title": "Published"}
    monkeypatch.setattr("app.zoj.remote", remote)
    library = owner.get(f"/api/zoj-transfer/problems/{problem}/packages/{job_id}")
    published = owner.post(f"/api/problems/{problem}/publish-zoj", json={
        "job_id": job_id, "contest_id": str(uuid.uuid4()),
        "division_id": str(uuid.uuid4()), "problem_code": "A"})
    if changed:
        assert library.status_code == published.status_code == 409
        assert library.json()["error"]["code"] == "library_package_stale"
        assert published.json()["error"]["code"] == "stale_package"
        assert calls == []
    else:
        assert library.status_code == 200, library.text
        assert published.status_code == 200, published.text
        assert len(calls) == 1
        with SessionLocal() as db:
            assert db.get(Problem, problem).document == legacy_document


def test_review_report_links_normalized_execution_to_unchanged_source_only(
        owner, problem, legacy_document):
    from app.db import now
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "validator-tests", "version": state["version"], "reuse": False})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.document_hash != job.options["source_document_hash"]
        job.status, job.finished_at = "completed", now()
        job.result = {"unit_tests": [{"id": "legacy", "matched": True,
                        "expected": "VALID", "actual": "VALID"}]}
        db.commit()
    report = owner.get(f"/api/problems/{problem}/review-report")
    assert report.status_code == 200, report.text
    assert job_id in report.json()["evidence_jobs"]
    state["document"]["info"]["name"] = "A genuine changed draft"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={
        "version": state["version"], "document": state["document"]})
    assert saved.status_code == 200, saved.text
    current = owner.get(f"/api/problems/{problem}/review-report")
    assert current.status_code == 200, current.text
    assert job_id not in current.json()["evidence_jobs"]
