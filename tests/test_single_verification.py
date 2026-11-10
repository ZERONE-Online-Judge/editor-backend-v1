import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql

from app.db import Access, Job, JobApplication, JobDiscard, Problem, Revision, SessionLocal, WorkingCopy
from app.demo import populate
from app.domain import document_hash, expanded_tests
from app.expectations import VERSION, evaluate
from app.invocation_scope import selected_testsets
from app.job_categories import category_condition
from app.pipeline import Pipeline
from app.single_verification import EXTENSIONS
from app.storage import blob_path, read_blob


CODE = "a,b=map(int,input().split())\nprint(a+b)\n"


def seeded(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
    return owner.get(f"/api/problems/{problem}").json()


def payload(version, *, language="python3", name=None, tag="OK", **options):
    name = name or "candidate" + EXTENSIONS.get(language, ".py")
    return {"kind": "verify", "version": version, "options": {"single_verification": True,
        "solutions": [name], "temporary_solutions": [{"name": name, "language": language, "tag": tag, "content": CODE}], **options}}


def requested(owner, problem, **options):
    state = seeded(owner, problem)
    response = owner.post(f"/api/problems/{problem}/jobs", json=payload(state["version"], **options))
    assert response.status_code == 202, response.text
    return state, response.json()


def finished(job, *, status="completed", verdict="AC"):
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        name = row.options["solutions"][0]
        source = row.document["solutions"][name]
        sets = selected_testsets(row.document, row.options, row.kind)
        indices = row.options.get("test_indices", [])
        prepared = {key: [copy.deepcopy(test) for test in expanded_tests(row.document["testsets"][key])
            if not indices or test["index"] in indices] for key in sets}
        cases = [{"testset": key, "index": test["index"], "verdict": verdict, "input_sha": test["input_sha"],
            "answer_sha": test["answer_sha"]} for key, tests in prepared.items() for test in tests]
        expected = evaluate(row.document, source, prepared, cases)
        row.status = status
        row.result = {"summary": "Done", "progress": 100, "verified": expected["matched"] and expected["expectations_complete"],
            "expectations_version": VERSION, "testsets": prepared, "solutions": [{"name": name, "tag": source["tag"],
                "state": "completed", "complete": True, "skipped": False, "cases": cases, **expected}]}
        db.commit()
        return copy.deepcopy(row.result)


def path(problem, job):
    return f"/api/problems/{problem}/jobs/{job['id']}"


@pytest.mark.parametrize("language", list(EXTENSIONS))
def test_single_request_has_only_one_selected_source_and_keeps_original_source_identity(owner, problem, language):
    state, job = requested(owner, problem, language=language)
    assert job["category"] == "verify-single"
    assert job["options"]["execution_backend"] == "zoj" and job["options"]["execution_transport"] == "agent"
    assert job["options"]["scope"]["solutions"] == ["candidate" + EXTENSIONS[language]]
    assert job["options"]["source_document_hash"] == state["document_hash"]
    assert "content" not in job["options"]["temporary_solutions"][0]
    assert owner.get(f"/api/problems/{problem}").json()["document"] == state["document"]
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        assert row.document_hash != state["document_hash"]
        assert row.document["solutions"][row.options["solutions"][0]]["tag"] == "OK"


@pytest.mark.parametrize("change", ["none", "two", "others", "empty_selection", "cpp20", "java17", "c17", "NR", "PE", "extension", "dots", "blank"])
def test_single_rejects_expanded_or_unsupported_request_before_queueing(owner, problem, change):
    state = seeded(owner, problem)
    values = payload(state["version"])
    options = values["options"]
    if change == "none": options["temporary_solutions"] = []
    elif change == "two": options["temporary_solutions"].append({**options["temporary_solutions"][0], "name": "other.py"})
    elif change == "others": options["solutions"].append("wrong.py")
    elif change == "empty_selection": options["solutions"] = []
    elif change in {"cpp20", "java17", "c17"}: options["temporary_solutions"][0]["language"] = change
    elif change in {"NR", "PE"}: options["temporary_solutions"][0]["tag"] = change
    elif change in {"extension", "dots"}:
        name = "candidate.cpp" if change == "extension" else "candidate.more.py"
        options["solutions"] = [name]
        options["temporary_solutions"][0]["name"] = name
    elif change == "blank": options["temporary_solutions"][0]["content"] = " \n\t"
    response = owner.post(f"/api/problems/{problem}/jobs", json=values)
    assert response.status_code == 400, response.text
    assert owner.get(f"/api/problems/{problem}/jobs").json() == []


def test_single_flag_cannot_be_used_for_other_kinds_and_filename_cannot_replace_files(owner, problem):
    state = seeded(owner, problem)
    for kind in ["prepare", "render", "stress", "package"]:
        values = {**payload(state["version"]), "kind": kind}
        assert owner.post(f"/api/problems/{problem}/jobs", json=values).status_code == 400
    values = payload(state["version"], name="main.cpp", language="cpp17")
    assert owner.post(f"/api/problems/{problem}/jobs", json=values).json()["error"]["code"] == "temporary_name_conflict"
    values = payload(state["version"])
    values["options"]["temporary_solutions"][0]["content"] = "한" * 400000
    assert owner.post(f"/api/problems/{problem}/jobs", json=values).status_code == 413


def test_categories_keep_full_and_single_separate_without_changing_kind_filter(owner, problem):
    state, job = requested(owner, problem)
    full = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": state["version"], "options": {"solutions": ["wrong.py"]}})
    assert full.status_code == 202, full.text
    assert full.json()["category"] == "verify" and "single_verification" not in full.json()["options"]
    for category, expected in [("verify-single", job["id"]), ("verify", full.json()["id"])]:
        result = owner.get(f"/api/problems/{problem}/jobs", params={"category": category, "page": 1, "page_size": 1}).json()
        assert result["total"] == 1 and result["items"][0]["id"] == expected
        compiled = select(Job).where(category_condition(category)).compile(dialect=postgresql.dialect())
        assert "single_verification" in compiled.params.values()
    assert len(owner.get(f"/api/problems/{problem}/jobs?kind=verify").json()) == 2


def test_existing_pipeline_judges_only_temporary_candidate_without_component_selftests(owner, problem, monkeypatch):
    _, job = requested(owner, problem)
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.job, pipeline.doc = row, copy.deepcopy(row.document)
    pipeline.remote_judging = True
    pipeline.result = {"testsets": {}, "solutions": [], "unit_tests": []}
    pipeline.result_lock = threading.RLock()
    pipeline.publish = lambda *_: None
    pipeline.event = lambda *_: None
    pipeline.sandbox = SimpleNamespace(check_cancelled=lambda: None, close=lambda: None)
    pipeline.prepare = lambda: pipeline.result.update(testsets={"tests": expanded_tests(pipeline.doc["testsets"]["tests"])})
    pipeline.unit_tests = lambda *_: (_ for _ in ()).throw(AssertionError("unrelated selftests"))
    seen = []
    def verify(_pipeline, _user, name, _cancellation):
        seen.append(name)
        source = pipeline.doc["solutions"][name]
        tests = pipeline.result["testsets"]["tests"]
        cases = [{"testset": "tests", "index": test["index"], "verdict": "AC", "input_sha": test["input_sha"], "answer_sha": test["answer_sha"]} for test in tests]
        return {"name": name, "tag": source["tag"], "complete": True, "cases": cases, **evaluate(pipeline.doc, source, pipeline.result["testsets"], cases)}
    monkeypatch.setattr("app.judge_bridge.verify_solution", verify)
    result = pipeline.run()
    assert seen == ["candidate.py"]
    assert [item["name"] for item in result["solutions"]] == ["candidate.py"] and result["unit_tests"] == []


def test_application_is_atomic_persistent_and_idempotent_without_committing_or_overwriting_later_edits(owner, problem):
    state, job = requested(owner, problem)
    finished(job)
    endpoint = path(problem, job)
    invalid = owner.post(endpoint + "/solution/apply", json={"version": state["version"], "content": "evil", "tag": "MA", "name": "evil.py"})
    assert invalid.status_code == 422
    applied = owner.post(endpoint + "/solution/apply", json={"version": state["version"]})
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["version"] == state["version"] + 1
    assert result["document_hash"] == document_hash(result["document"]) == result["application"]["document_hash"]
    source = result["document"]["solutions"]["candidate.py"]
    assert source == result["solution"] and source["tag"] == "OK" and read_blob(source["sha256"]).decode() == CODE
    assert owner.post(endpoint + "/solution/apply", json={"version": state["version"]}).json() == result
    assert owner.get(endpoint).json()["application"] == result["application"]
    assert owner.post(endpoint + "/discard").json()["error"]["code"] == "already_applied"
    current = owner.get(f"/api/problems/{problem}").json()
    current["document"]["info"]["name"] = "Later change"
    owner.put(f"/api/problems/{problem}/working-copy", json={"version": current["version"], "document": current["document"]})
    assert owner.post(endpoint + "/solution/apply", json={"version": state["version"]}).json()["document"]["info"]["name"] == "Later change"
    with SessionLocal() as db:
        assert db.get(Problem, problem).document == state["document"]


@pytest.mark.parametrize("change", ["verified", "partial", "mismatch", "missing_case", "duplicate_case", "wrong_set", "input", "extra_solution", "CE", "JUDGE_ERROR"])
def test_incomplete_or_mismatched_evidence_cannot_be_applied(owner, problem, change):
    state, job = requested(owner, problem)
    result = finished(job)
    row = result["solutions"][0]
    if change == "verified": result["verified"] = False
    elif change == "partial": row["complete"] = False
    elif change == "mismatch": row["cases"][0]["verdict"] = "WA"
    elif change == "missing_case": row["cases"].pop()
    elif change == "duplicate_case": row["cases"][0] = copy.deepcopy(row["cases"][1])
    elif change == "wrong_set": row["cases"][0]["testset"] = "unrequested"
    elif change == "input": row["cases"][0]["input_sha"] = "a" * 64
    elif change == "extra_solution": result["solutions"].append(copy.deepcopy(row))
    else: row["cases"][0]["verdict"] = change
    with SessionLocal() as db:
        db.get(Job, job["id"]).result = result
        db.commit()
    response = owner.post(path(problem, job) + "/solution/apply", json={"version": state["version"]})
    assert response.status_code == 400, response.text
    assert "candidate.py" not in owner.get(f"/api/problems/{problem}").json()["document"]["solutions"]


@pytest.mark.parametrize("collision,old_version", [(False, False), (False, True), (True, False)])
def test_stale_copy_and_name_collisions_preserve_all_current_data(owner, problem, collision, old_version):
    state, job = requested(owner, problem)
    finished(job)
    current = owner.get(f"/api/problems/{problem}").json()
    if collision:
        current["document"]["solutions"]["candidate.py"] = {**current["document"]["solutions"]["wrong.py"], "name": "candidate.py"}
    else: current["document"]["info"]["name"] = "New source"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": current["version"], "document": current["document"]}).json()
    result = owner.post(path(problem, job) + "/solution/apply", json={"version": state["version"] if old_version else saved["version"]})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == ("workspace_changed" if old_version else "stale_results")
    assert owner.get(f"/api/problems/{problem}").json()["document"] == current["document"]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_discard_is_personal_persistent_idempotent_and_blocks_apply_and_reuse(owner, problem, status):
    state, job = requested(owner, problem)
    finished(job, status=status, verdict="WA")
    endpoint = path(problem, job)
    result = owner.post(endpoint + "/discard")
    assert result.status_code == 200, result.text
    assert owner.post(endpoint + "/discard").json() == result.json()
    assert owner.get(endpoint).json()["discarded_at"] == result.json()["discarded_at"]
    assert owner.post(endpoint + "/solution/apply", json={"version": state["version"]}).json()["error"]["code"] == "result_discarded"
    assert owner.get(f"/api/problems/{problem}").json()["document"] == state["document"]
    assert owner.post(f"/api/problems/{problem}/jobs", json=payload(state["version"])).json()["id"] != job["id"]


def test_readonly_reviewer_executes_committed_code_but_cannot_adopt_or_discard_and_other_users_stay_isolated(owner, problem, clients, client):
    state = seeded(owner, problem)
    reviewer = clients("single-reviewer")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "single-reviewer", "role": "READ", "reviewer": True})
    response = reviewer.post(f"/api/problems/{problem}/jobs", json=payload(0, snapshot="committed"))
    assert response.status_code == 202, response.text
    job = response.json()
    finished(job)
    endpoint = path(problem, job)
    for suffix, values in [("/solution/apply", {"version": 0}), ("/discard", None)]:
        assert reviewer.post(endpoint + suffix, json=values).status_code == 403
        assert client.post(endpoint + suffix, json=values).status_code == 401
    outsider = clients("single-outsider")
    assert outsider.post(endpoint + "/solution/apply", json={"version": 0}).status_code == 404
    assert owner.post(endpoint + "/solution/apply", json={"version": state["version"]}).status_code == 200


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_noncompleted_jobs_never_apply_and_active_jobs_cannot_discard(owner, problem, status):
    state, job = requested(owner, problem)
    finished(job, status=status)
    endpoint = path(problem, job)
    assert owner.post(endpoint + "/solution/apply", json={"version": state["version"]}).status_code == 400
    if status in {"queued", "running"}: assert owner.post(endpoint + "/discard").status_code == 400


@pytest.mark.parametrize("missing", [False, True])
def test_corrupted_or_missing_candidate_blob_cannot_be_adopted(owner, problem, missing):
    state, job = requested(owner, problem)
    finished(job)
    sha = job["options"]["temporary_solutions"][0]["sha256"]
    target, original = blob_path(sha), read_blob(sha)
    try:
        if missing: target.unlink()
        else: target.write_bytes(b"x" * len(original))
        result = owner.post(path(problem, job) + "/solution/apply", json={"version": state["version"]})
        assert result.status_code == 409 and result.json()["error"]["code"] == "invalid_solution_blob"
    finally: target.write_bytes(original)


def test_same_source_after_version_changes_can_adopt_a_committed_run(owner, problem):
    state, job = requested(owner, problem, snapshot="committed")
    doc = copy.deepcopy(state["document"])
    doc["info"]["name"] = "Changed and restored"
    first = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": doc}).json()
    restored = owner.put(f"/api/problems/{problem}/working-copy", json={"version": first["version"], "document": state["document"]}).json()
    assert restored["version"] > job["workspace_version"]
    finished(job)
    applied = owner.post(path(problem, job) + "/solution/apply", json={"version": restored["version"]})
    assert applied.status_code == 200, applied.text


def test_all_testsets_keep_independent_case_identities_and_partial_selection_never_looks_complete(owner, problem):
    state = seeded(owner, problem)
    state["document"]["testsets"]["additional"] = copy.deepcopy(state["document"]["testsets"]["tests"])
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]}).json()
    requested_all = owner.post(f"/api/problems/{problem}/jobs", json=payload(saved["version"], all_testsets=True)).json()
    result = finished(requested_all)
    assert len(result["solutions"][0]["cases"]) == 6
    applied = owner.post(path(problem, requested_all) + "/solution/apply", json={"version": saved["version"]})
    assert applied.status_code == 200, applied.text
    current = owner.get(f"/api/problems/{problem}").json()
    partial = owner.post(f"/api/problems/{problem}/jobs", json=payload(current["version"], name="partial.py", test_indices=[1])).json()
    result = finished(partial)
    # Even optimistic worker flags cannot turn a subset into a complete
    # expected default scope; the server recomputes it from saved cases.
    result["verified"] = True
    result["solutions"][0].update(matched=True, expectations_complete=True)
    with SessionLocal() as db:
        db.get(Job, partial["id"]).result = result
        db.commit()
    rejected = owner.post(path(problem, partial) + "/solution/apply", json={"version": current["version"]})
    assert rejected.status_code == 400


def test_apply_and_discard_records_are_personal_and_use_current_solution_permission(owner, problem, clients, monkeypatch):
    state, job = requested(owner, problem)
    finished(job)
    writer = clients("single-writer")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "single-writer", "role": "WRITE"})
    current = writer.get(f"/api/problems/{problem}").json()
    assert writer.post(path(problem, job) + "/solution/apply", json={"version": current["version"]}).status_code == 200
    assert owner.get(path(problem, job)).json()["application"] is None
    assert owner.post(path(problem, job) + "/discard").status_code == 200
    assert writer.get(path(problem, job)).json()["discarded_at"] is None
    assert "candidate.py" not in owner.get(f"/api/problems/{problem}").json()["document"]["solutions"]
    other = owner.post(f"/api/problems/{problem}/jobs", json=payload(state["version"], name="after_revoke.py")).json()
    finished(other)
    # Revoke after the first authorization, immediately before obtaining the
    # user lock. Its fresh permission read must reject both mutation paths.
    from app import folders
    original = folders.lock_user
    def revoke_then_lock(db, user):
        with SessionLocal() as fresh:
            access = fresh.get(Access, (problem, user.id))
            access.role, access.reviewer, access.permissions = "READ", True, None
            fresh.commit()
        original(db, user)
    monkeypatch.setattr(folders, "lock_user", revoke_then_lock)
    result = writer.post(path(problem, other) + "/solution/apply", json={"version": current["version"] + 1})
    assert result.status_code == 403
    assert writer.post(path(problem, other) + "/discard").status_code == 403


@pytest.mark.parametrize("discard", [False, True])
def test_concurrent_duplicate_apply_or_discard_race_has_one_persistent_winner(owner, problem, discard):
    state, job = requested(owner, problem)
    finished(job)
    endpoint = path(problem, job)
    def apply(): return owner.post(endpoint + "/solution/apply", json={"version": state["version"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(apply)
        second = pool.submit(lambda: owner.post(endpoint + "/discard") if discard else apply())
        results = [first.result(), second.result()]
    assert sorted(response.status_code for response in results) == ([200, 409] if discard else [200, 200])
    with SessionLocal() as db:
        applications = db.scalar(select(func.count()).select_from(JobApplication).where(JobApplication.job_id == job["id"]))
        discards = db.scalar(select(func.count()).select_from(JobDiscard).where(JobDiscard.job_id == job["id"]))
        assert applications + discards == 1
        wc = db.scalar(select(WorkingCopy).where(WorkingCopy.problem_id == problem))
        assert wc.version == state["version"] + applications
        assert ("candidate.py" in wc.document["solutions"]) == bool(applications)
