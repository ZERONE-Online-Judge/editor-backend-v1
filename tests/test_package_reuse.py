"""Downloading unchanged package artifacts must not request fresh execution."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db import Job, Problem, Revision, SessionLocal, engine
from app.domain import document_hash
from app.jobs import JobOptions
from app.packages import build_package
from app.package_reuse import package_path
from test_feature_permissions import grant
from test_folder_package_exports import add, folder, start, finish


def save_package(owner, *, export_format="zoj", verify=True, extra_sets=False, explicit=None):
    root = folder(owner)
    pid = add(owner, root, ready=True)
    with SessionLocal() as db:
        problem = db.get(Problem, pid)
        doc = copy.deepcopy(problem.document)
        doc["statements"]["english"] = copy.deepcopy(doc["statements"]["korean"])
        if extra_sets:
            doc["testsets"]["other"] = copy.deepcopy(doc["testsets"]["tests"])
        problem.document = doc
        db.get(Revision, (pid, 0)).document = copy.deepcopy(doc)
        options = JobOptions(export_format=export_format, verify=verify, testsets=explicit).model_dump()
        options.update(snapshot="committed", source_document_hash=document_hash(doc), source_revision=0,
            runtime_revision="previous-backend", execution_backend="old-agent", execution_transport="old-transport",
            folder_export_id="previous-folder", permission_mode="reviewer")
        # Deliberately omit modern protocols and top-level verified summary:
        # the completed historical artifact already records verification.
        job = Job(problem_id=pid, user_id=problem.owner_id, kind="package", status="completed", document=doc,
            document_hash=document_hash(doc), revision=0, workspace_version=0, options=options)
        db.add(job)
        db.flush()
        artifact = build_package(job, {name: list(config["tests"].values()) for name, config in doc["testsets"].items()})
        job.result = {"package": artifact}
        db.commit()
        return root, pid, job.id


def request(owner, pid, *, reuse=True, **options):
    state = owner.get(f"/api/problems/{pid}").json()
    return owner.post(f"/api/problems/{pid}/jobs", json={"kind": "package", "version": state["version"],
        "reuse": reuse, "options": {"export_format": "zoj", **options}})


def no_agents(*args, **kwargs):
    raise HTTPException(503, {"code": "agents_offline", "message": "Offline"})


def test_individual_reuses_historical_verified_package_without_agent_calls(owner, monkeypatch):
    _, pid, original = save_package(owner)
    monkeypatch.setattr(get_settings(), "runtime_revision", "new-backend")
    def unexpected(*args, **kwargs):
        pytest.fail("Existing package reuse must not request capabilities")
    monkeypatch.setattr("app.judge_bridge.capabilities", unexpected)
    response = request(owner, pid)
    assert response.status_code == 202, response.text
    assert response.json()["id"] == original and response.json()["reused"] is True
    assert response.json()["result"]["package"]["verified"] is True
    with SessionLocal() as db:
        assert len(db.scalars(select(Job)).all()) == 1
        assert db.get(Job, original).result.keys() == {"package"}


def test_skip_new_verification_preserves_existing_verified_metadata(owner, monkeypatch):
    root, pid, original = save_package(owner)
    monkeypatch.setattr("app.judge_bridge.capabilities", no_agents)
    response = request(owner, pid, verify=False)
    assert response.status_code == 202 and response.json()["id"] == original
    assert response.json()["options"]["verify"] is True
    assert response.json()["result"]["package"]["verified"] is True
    batch = finish(owner, start(owner, root, export_format="zoj", verify=False)["id"])
    assert batch["ready"] == 1 and batch["items"][0]["job_id"] == original


@pytest.mark.parametrize("change", ["format", "language", "scope", "layout", "source", "revision", "unverified", "missing", "invalid", "force"])
def test_incompatible_or_forced_package_requires_fresh_agent_admission(owner, monkeypatch, change):
    _, pid, original = save_package(owner, verify=change != "unverified", extra_sets=True)
    options, reuse = {}, True
    if change == "format":
        options["export_format"] = "editor"
    elif change == "language":
        options["statement_language"] = "english"
    elif change == "scope":
        options["testsets"] = ["tests", "other"]
    elif change == "layout":
        options["testsets"] = ["tests"]
    elif change == "force":
        reuse = False
    elif change == "missing":
        package_path(original).unlink()
    elif change in {"source", "revision", "invalid"}:
        with SessionLocal() as db:
            problem = db.get(Problem, pid)
            if change == "source":
                doc = copy.deepcopy(problem.document)
                doc["info"]["name"] = "Changed source"
                problem.document = doc
                db.get(Revision, (pid, 0)).document = copy.deepcopy(doc)
            elif change == "revision":
                problem.revision = 1
                db.add(Revision(problem_id=pid, number=1, document=copy.deepcopy(problem.document), author_id=problem.owner_id, message="New"))
            else:
                job = db.get(Job, original)
                job.result = {**job.result, "package": {**job.result["package"], "invalid": True}}
            db.commit()
    monkeypatch.setattr("app.judge_bridge.capabilities", no_agents)
    response = request(owner, pid, reuse=reuse, **options)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "agents_offline"


def test_folder_force_rebuild_is_separate_from_verification_and_fingerprint(owner):
    root, pid, original = save_package(owner, export_format="editor", verify=False)
    default = start(owner, root)
    forced = start(owner, root, reuse=False)
    assert default["id"] != forced["id"]
    assert forced["options"]["reuse"] is False
    completed = finish(owner, default["id"])
    assert completed["items"][0]["job_id"] == original
    rebuilt = finish(owner, forced["id"])
    assert rebuilt["items"][0]["job_id"] != original
    assert rebuilt["items"][0]["reused"] is False


@pytest.mark.parametrize("options,code", [({"execution_backend": "editor"}, "judge_backend_required"),
    ({"test_indices": [1]}, "invalid_options"), ({"prepare_mode": "inputs"}, "invalid_options")])
def test_existing_artifact_does_not_bypass_static_request_validation(owner, options, code):
    _, pid, _ = save_package(owner)
    response = request(owner, pid, **options)
    assert response.status_code == 400 and response.json()["error"]["code"] == code


def test_live_package_permission_is_checked_before_individual_reuse(owner, clients):
    _, pid, _ = save_package(owner)
    member = clients("member")
    grant(owner, pid, "member", [])
    response = request(member, pid)
    assert response.status_code == 403


def test_concurrent_reuse_requests_attach_to_one_new_package_job(owner, monkeypatch):
    from app.reference_generation import PROTOCOL
    root = folder(owner)
    pid = add(owner, root, ready=True)
    barrier, lock, seen = threading.Barrier(2), threading.Lock(), set()
    def capabilities(user):
        ident = threading.get_ident()
        with lock:
            first = ident not in seen
            seen.add(ident)
        if first:
            barrier.wait(timeout=10)
        return {"runtime_revision": "current-agent", "reference_output_protocol": PROTOCOL,
            "reference_output_online_nodes": 1, "execution": {"runtime_revision": "current-execution",
                "protocol": "editor-execute-v1", "online_nodes": 1}}
    monkeypatch.setattr("app.judge_bridge.capabilities", capabilities)
    # Create the workspace before concurrent requests, so only job admission
    # and its second cache lookup participate in this race.
    owner.get(f"/api/problems/{pid}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: request(owner, pid), range(2)))
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert sorted(response.json().get("reused", False) for response in responses) == [False, True]
    with SessionLocal() as db:
        assert len(db.scalars(select(Job)).all()) == 1


def test_single_download_rejects_same_size_corruption_and_retry_regenerates(owner, monkeypatch):
    from app import worker
    _, pid, original = save_package(owner)
    path = package_path(original)
    content = path.read_bytes()
    damaged = bytes([content[0] ^ 255]) + content[1:]
    path.write_bytes(damaged)
    real_read = type(path).read_bytes
    def read(candidate):
        if candidate == path:
            assert engine.pool.checkedout() == 0
        return real_read(candidate)
    monkeypatch.setattr(type(path), "read_bytes", read)
    response = owner.get(f"/api/problems/{pid}/packages/{original}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "package_artifact_corrupt"
    with SessionLocal() as db:
        assert db.get(Job, original).result["package"]["invalid"] is True
    monkeypatch.setattr("app.judge_bridge.capabilities", no_agents)
    assert request(owner, pid).status_code == 503
    rebuilt = request(owner, pid, verify=False)
    assert rebuilt.status_code == 202 and rebuilt.json()["id"] != original
    assert rebuilt.json().get("reused") is not True
    job = worker.claim_job()
    worker.process_job(job)
    assert owner.get(f"/api/problems/{pid}/packages/{job.id}").status_code == 200
