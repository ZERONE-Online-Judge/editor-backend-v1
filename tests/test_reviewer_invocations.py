import copy
import pytest
from sqlalchemy import select
from app.db import Access, Job, JobComment, Problem, Revision, SessionLocal, WorkingCopy
from app.demo import populate
from app.domain import document_hash
from app.worker import claim_job, process_job


def committed_problem(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
    return owner.get(f"/api/problems/{problem}").json()


def reviewer(owner, clients, problem):
    user = clients("reviewer")
    response = owner.put(f"/api/problems/{problem}/accesses", json={"login": "reviewer", "role": "READ", "reviewer": True})
    assert response.status_code == 200
    return user


def test_reviewer_uses_committed_files_and_cannot_change_sources(owner, problem, clients):
    original = committed_problem(owner, problem)
    user = reviewer(owner, clients, problem)
    changed = copy.deepcopy(original["document"])
    changed["solutions"]["wrong.py"]["tag"] = "OK"
    changed["statements"]["korean"]["legend"] = "Uncommitted author changes"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": original["version"], "document": changed})
    assert saved.status_code == 200
    requested = user.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": 0, "options": {"solutions": ["wrong.py"]}})
    assert requested.status_code == 202, requested.text
    with SessionLocal() as db:
        row = db.get(Job, requested.json()["id"])
        assert row.document == original["document"]
        assert row.options["snapshot"] == "committed"
    assert user.put(f"/api/problems/{problem}/working-copy", json={"version": 0, "document": changed}).status_code == 403
    assert user.post(f"/api/problems/{problem}/blobs", content=b"replacement").status_code == 403
    assert user.post(f"/api/problems/{problem}/jobs/{requested.json()['id']}/apply", json={"version": 0}).status_code == 403
    assert owner.get(f"/api/problems/{problem}").json()["document"] == changed


@pytest.mark.parametrize("kind,options", [("prepare", {}), ("package", {}), ("verify", {"snapshot": "working_copy"}), ("verify", {"auto_apply": True})])
def test_reviewer_cannot_expand_write_permission(owner, problem, clients, kind, options):
    committed_problem(owner, problem)
    user = reviewer(owner, clients, problem)
    response = user.post(f"/api/problems/{problem}/jobs", json={"kind": kind, "version": 0, "options": options})
    assert response.status_code == 403


def test_reviewer_temporary_source_is_job_only(owner, problem, clients):
    original = committed_problem(owner, problem)
    user = reviewer(owner, clients, problem)
    response = user.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": 0, "options": {
        "solutions": ["review.py"], "temporary_solutions": [{"name": "review.py", "language": "python3", "tag": "OK", "content": "print(42)\n"}],
    }})
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        row = db.get(Job, response.json()["id"])
        assert "review.py" in row.document["solutions"]
        assert db.get(Problem, problem).document == original["document"]
        assert row.options["source_document_hash"] == original["document_hash"]
        assert "content" not in row.options["temporary_solutions"][0]
        assert db.get(WorkingCopy, (problem, row.user_id)) is None


@pytest.mark.parametrize("name,status", [("main.cpp", 400), ("validator.cpp", 400), ("testlib.h", 422), ("../tool.py", 422)])
def test_temporary_code_cannot_replace_committed_or_platform_files(owner, problem, clients, name, status):
    committed_problem(owner, problem)
    user = reviewer(owner, clients, problem)
    response = user.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": 0, "options": {
        "temporary_solutions": [{"name": name, "language": "python3", "content": "print(42)"}],
    }})
    assert response.status_code == status, response.text


def test_plain_reader_cannot_invoke_or_comment(owner, problem, clients):
    reader = clients("reader")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "reader", "role": "READ"})
    job = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0}).json()
    assert reader.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0}).status_code == 403
    assert reader.post(f"/api/problems/{problem}/jobs/{job['id']}/comments", json={"content": "no permission"}).status_code == 403


def test_invocation_comments_survive_worker_updates_and_are_shared(owner, problem, clients):
    user = reviewer(owner, clients, problem)
    job = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0}).json()
    path = f"/api/problems/{problem}/jobs/{job['id']}/comments"
    response = user.post(path, json={"content": "  확인한 판정과 검수 의견  "})
    assert response.status_code == 201
    with SessionLocal() as db:
        row = db.get(Job, job['id'])
        row.result = {"summary": "Worker updated the result"}
        db.commit()
        assert db.scalar(select(JobComment)).content == "확인한 판정과 검수 의견"
    assert owner.get(path).json()[0]["author"] == "reviewer"
    assert user.post(path, json={"content": " "}).status_code == 400
    stranger = clients("stranger")
    assert stranger.get(path).status_code == 404


def test_reviewer_can_cancel_only_own_invocation(owner, problem, clients):
    user = reviewer(owner, clients, problem)
    theirs = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0}).json()
    own = user.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0}).json()
    assert user.post(f"/api/problems/{problem}/jobs/{theirs['id']}/cancel").status_code == 403
    assert user.post(f"/api/problems/{problem}/jobs/{own['id']}/cancel").status_code == 200


def test_revoked_reviewer_cannot_continue_running(owner, problem, clients, monkeypatch):
    user = reviewer(owner, clients, problem)
    response = user.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0})
    claimed = claim_job()
    assert claimed.id == response.json()["id"]
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "reviewer", "role": "READ", "reviewer": False})
    class CheckedPipeline:
        def __init__(self, job, publish, check): self.check = check
        def run(self): self.check(); raise AssertionError("revoked code must not run")
    monkeypatch.setattr("app.worker.Pipeline", CheckedPipeline)
    process_job(claimed)
    assert user.get(f"/api/problems/{problem}/jobs/{claimed.id}").json()["status"] == "cancelled"
