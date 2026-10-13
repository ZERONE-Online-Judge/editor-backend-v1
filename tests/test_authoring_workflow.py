import copy
import pytest
from app.db import Access, Job, JobApplication, SessionLocal
from app.domain import document_hash
from app.schema import Document
from app.worker import claim_job, process_job


def fixture_job(owner, problem, *, auto=False, result_changed=True):
    state = owner.get(f"/api/problems/{problem}").json()
    inp = owner.post(f"/api/problems/{problem}/blobs", content=b"1 2\n").json()["sha256"]
    ans = owner.post(f"/api/problems/{problem}/blobs", content=b"3\n").json()["sha256"]
    test = {"index": 1, "input_sha": inp, "answer_sha": None if result_changed else ans,
            "command": "", "group": "", "points": 0, "description": "예제", "sample": True}
    state["document"]["testsets"]["tests"]["tests"] = {"fixture": test}
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert saved.status_code == 200
    requested = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "prepare", "version": saved.json()["version"], "options": {"auto_apply": auto}})
    assert requested.status_code == 202, requested.text
    prepared = {"summary": "생성 완료", "testsets": {"tests": [{**test, "answer_sha": ans, "validator": "VALID"}]}}
    return requested.json(), prepared, saved.json()["version"]


def complete(job, result):
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        row.status, row.result = "completed", result
        db.commit()


def test_apply_is_persistent_idempotent_and_removes_pending(owner, problem):
    job, prepared, version = fixture_job(owner, problem)
    complete(job, prepared)
    assert owner.get(f"/api/problems/{problem}/preparation-status").json()["pending_preparations"][0]["stale"] is False
    response = owner.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": version})
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["application"]["workspace_version"] == version + 1
    state = owner.get(f"/api/problems/{problem}").json()
    assert state["document"]["testsets"]["tests"]["tests"]["fixture"]["answer_sha"] == prepared["testsets"]["tests"][0]["answer_sha"]
    assert state["pending_preparations"] == []
    assert owner.get(f"/api/problems/{problem}/jobs").json()[0]["application"] == applied["application"]
    assert owner.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": version}).json() == applied
    # Re-applying an old job cannot overwrite edits made after applying it.
    state["document"]["info"]["name"] = "새 편집"
    owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    owner.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": version})
    assert owner.get(f"/api/problems/{problem}").json()["document"]["info"]["name"] == "새 편집"


def test_unchanged_output_still_records_application(owner, problem):
    job, result, version = fixture_job(owner, problem, result_changed=False)
    complete(job, result)
    response = owner.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": version})
    assert response.status_code == 200
    assert response.json()["version"] == version
    assert response.json()["application"]
    assert owner.get(f"/api/problems/{problem}").json()["pending_preparations"] == []


@pytest.mark.parametrize("stale_version", [False, True])
def test_stale_application_cannot_overwrite_newer_copy(owner, problem, stale_version):
    job, result, version = fixture_job(owner, problem)
    complete(job, result)
    state = owner.get(f"/api/problems/{problem}").json()
    state["document"]["info"]["name"] = "편집 유지"
    next_version = owner.put(f"/api/problems/{problem}/working-copy", json={"version": version, "document": state["document"]}).json()["version"]
    response = owner.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": version if stale_version else next_version})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("workspace_changed" if stale_version else "stale_results")
    assert owner.get(f"/api/problems/{problem}").json()["document"]["testsets"]["tests"]["tests"]["fixture"]["answer_sha"] is None


def test_application_belongs_to_personal_copy_and_requires_write(owner, problem, clients):
    job, result, version = fixture_job(owner, problem)
    owner.post(f"/api/problems/{problem}/commit", json={"version": version, "message": "자료 공유"})
    complete(job, result)
    writer = clients("writer")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "writer", "role": "WRITE"})
    writer_version = writer.get(f"/api/problems/{problem}").json()["version"]
    assert writer.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": writer_version}).status_code == 200
    assert writer.get(f"/api/problems/{problem}/jobs/{job['id']}").json()["application"]
    assert owner.get(f"/api/problems/{problem}/jobs/{job['id']}").json()["application"] is None
    assert owner.get(f"/api/problems/{problem}").json()["document"]["testsets"]["tests"]["tests"]["fixture"]["answer_sha"] is None
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "writer", "role": "READ"})
    assert writer.post(f"/api/problems/{problem}/jobs/{job['id']}/apply", json={"version": 0}).status_code == 403
    assert writer.get(f"/api/problems/{problem}/preparation-status").json()["pending_preparations"] == []


@pytest.mark.parametrize("changed,failed,revoked", [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_worker_auto_apply_is_guarded(owner, problem, monkeypatch, changed, failed, revoked):
    job, result, version = fixture_job(owner, problem, auto=True)
    claimed = claim_job()
    def execute(*args):
        if changed:
            state = owner.get(f"/api/problems/{problem}").json()
            state["document"]["info"]["name"] = "다른 창 편집"
            owner.put(f"/api/problems/{problem}/working-copy", json={"version": version, "document": state["document"]})
        if revoked:
            with SessionLocal() as db:
                access = db.get(Access, (problem, claimed.user_id))
                access.role = "READ"
                db.commit()
        if failed:
            raise RuntimeError("validator failed")
        return result
    monkeypatch.setattr("app.worker.Pipeline", lambda *args: type("FixturePipeline", (), {"run": execute})())
    process_job(claimed)
    with SessionLocal() as db:
        applied = db.get(JobApplication, (job["id"], claimed.user_id))
        row = db.get(Job, job["id"])
        assert bool(applied) == (not changed and not failed and not revoked)
        assert row.status == ("failed" if failed else "completed")
        if not failed:
            assert row.result["auto_apply"]["status"] == ("blocked" if changed or revoked else "applied")


def test_commit_preview_includes_unsaved_text_without_saving(owner, problem):
    state = owner.get(f"/api/problems/{problem}").json()
    doc = copy.deepcopy(state["document"])
    doc["statements"]["korean"]["legend"] = "새 지문 $N$"
    result = owner.post(f"/api/problems/{problem}/commit-preview", json={"version": state["version"], "document": doc})
    assert result.status_code == 200, result.text
    assert result.json()["before"] == state["document"]
    assert result.json()["after"] == doc
    current = owner.get(f"/api/problems/{problem}").json()
    assert current["document"] == state["document"] and current["version"] == state["version"]


def test_cancelled_generation_does_not_auto_apply(owner, problem, monkeypatch):
    job, result, _ = fixture_job(owner, problem, auto=True)
    claimed = claim_job()
    def execute(*args):
        assert owner.post(f"/api/problems/{problem}/jobs/{job['id']}/cancel").status_code == 200
        return result
    monkeypatch.setattr("app.worker.Pipeline", lambda *args: type("FixturePipeline", (), {"run": execute})())
    process_job(claimed)
    with SessionLocal() as db:
        assert db.get(Job, job["id"]).status == "cancelled"
        assert db.get(JobApplication, (job["id"], claimed.user_id)) is None


def test_preview_merges_peer_changes_and_detects_conflicts(owner, problem, clients):
    writer = clients("writer")
    owner.put(f"/api/problems/{problem}/accesses", json={"login": "writer", "role": "WRITE"})
    ours = owner.get(f"/api/problems/{problem}").json()
    theirs = writer.get(f"/api/problems/{problem}").json()
    theirs["document"]["info"]["name"] = "동료의 제목"
    version = writer.put(f"/api/problems/{problem}/working-copy", json={"version": theirs["version"], "document": theirs["document"]}).json()["version"]
    writer.post(f"/api/problems/{problem}/commit", json={"version": version, "message": "제목"})
    ours["document"]["statements"]["korean"]["legend"] = "내 지문"
    preview = owner.post(f"/api/problems/{problem}/commit-preview", json={"version": ours["version"], "document": ours["document"]}).json()
    assert preview["before"]["info"]["name"] == preview["after"]["info"]["name"] == "동료의 제목"
    assert preview["conflicts"] == []
    ours["document"]["info"]["name"] = "내 제목"
    assert owner.post(f"/api/problems/{problem}/commit-preview", json={"version": ours["version"], "document": ours["document"]}).json()["conflicts"] == ["info/name"]
    assert owner.post(f"/api/problems/{problem}/commit", json={"version": ours["version"], "message": "커밋", "expected_revision": 0}).json()["error"]["code"] == "preview_changed"


def test_role_metadata_preserves_legacy_hashes_and_accepts_multiple_roles(owner, problem):
    state = owner.get(f"/api/problems/{problem}").json()
    blob = owner.post(f"/api/problems/{problem}/blobs", content=b"int main(){}\n").json()
    state["document"]["files"]["tool.cpp"] = {"name": "tool.cpp", "kind": "source", "language": "cpp17", "compile_with_solution": False, "entry_class": "", **blob}
    doc = state["document"]
    assert document_hash(Document.model_validate(doc).model_dump()) == document_hash(doc)
    doc["files"]["tool.cpp"]["roles"] = ["validator", "generator"]
    response = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": doc})
    assert response.status_code == 200, response.text
    assert owner.get(f"/api/problems/{problem}").json()["document"]["files"]["tool.cpp"]["roles"] == ["validator", "generator"]
    doc["files"]["tool.cpp"]["kind"] = "resource"
    assert owner.put(f"/api/problems/{problem}/working-copy", json={"version": response.json()["version"], "document": doc}).status_code == 422
