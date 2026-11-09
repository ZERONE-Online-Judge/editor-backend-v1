import copy
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from fastapi import HTTPException

from app import folder_package_exports as exports, worker
from app.config import get_settings
from app.db import FolderPackageExport, Job, Placement, Problem, Revision, SessionLocal, WorkingCopy, engine
from app.demo import populate
from app.domain import document_hash
from app.jobs import JobOptions
from app.packages import build_package
from test_feature_permissions import grant, team
from test_shared_folders import shared, add_problem


def folder(owner, name="자료", parent=None):
    response = owner.post("/api/folders", json={"name": name, "parent_id": parent})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add(owner, target, index=0, ready=False):
    response = owner.post("/api/problems", json={"name": f"문제 {index}", "slug": f"problem-{index}", "folder_id": target})
    assert response.status_code == 201, response.text
    pid = response.json()["id"]
    if ready:
        with SessionLocal() as db:
            problem = db.get(Problem, pid)
            problem.document = populate(db, problem)
            db.get(Revision, (pid, 0)).document = copy.deepcopy(problem.document)
            db.commit()
    return pid


def start(owner, target, **options):
    response = owner.post(f"/api/folders/{target}/package-exports", json={"verify": False, "export_format": "editor", **options})
    assert response.status_code == 202, response.text
    return response.json()


def finish(owner, ident):
    for _ in range(30):
        exports.advance_exports()
        while job := worker.claim_job():
            worker.process_job(job)
        response = owner.get(f"/api/folder-package-exports/{ident}")
        assert response.status_code == 200, response.text
        if response.json()["status"] not in exports.ACTIVE:
            return response.json()
    pytest.fail("Export did not finish")


def forbid_execution(*args, **kwargs):
    pytest.fail("Archive-only export must not contact or initialize execution")


def test_folder_export_recurses_dedupes_and_survives_queue_capacity(owner, monkeypatch):
    monkeypatch.setattr(worker, "Pipeline", forbid_execution)
    monkeypatch.setattr("app.judge_bridge.capabilities", forbid_execution)
    root = folder(owner)
    child = folder(owner, "하위", root)
    ids = [add(owner, root if index < 3 else child, index) for index in range(7)]
    batch = start(owner, root)
    assert batch["total"] == 7
    assert all(isinstance(item["folder_path"], list) for item in batch["items"])
    assert start(owner, root)["id"] == batch["id"]
    exports.advance_exports()
    exports.advance_exports()
    exports.advance_exports()
    with SessionLocal() as db:
        assert len(db.scalars(select(Job).where(Job.status.in_(exports.ACTIVE))).all()) == 2
        assert db.scalar(select(WorkingCopy).where(WorkingCopy.problem_id.in_(ids))) is None
    completed = finish(owner, batch["id"])
    assert (completed["status"], completed["ready"], completed["failed"]) == ("completed", 7, 0)
    download = owner.get(completed["download_url"])
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["total"] == 7
        assert len(archive.namelist()) == 8
        assert all(entry.compress_type == zipfile.ZIP_STORED for entry in archive.infolist())
        assert any("하위" in name for name in archive.namelist())
        assert all(not name.startswith("/") and ".." not in name.split("/") for name in archive.namelist())
    shallow = start(owner, root, include_descendants=False)
    assert shallow["total"] == 3
    assert len(owner.get(f"/api/folders/{root}/package-exports").json()["items"]) == 2


def test_committed_snapshot_ignores_dirty_work_and_later_commits_and_moves(owner):
    root, destination = folder(owner), folder(owner, "다른 폴더")
    pid = add(owner, root)
    state = owner.get(f"/api/problems/{pid}").json()
    dirty = copy.deepcopy(state["document"])
    dirty["info"]["name"] = "비공개 미완성"
    response = owner.put(f"/api/problems/{pid}/working-copy", json={"version": state["version"], "document": dirty})
    assert response.status_code == 200
    batch = start(owner, root)
    with SessionLocal() as db:
        problem = db.get(Problem, pid)
        newer = copy.deepcopy(problem.document)
        newer["info"]["name"] = "새 커밋"
        problem.document, problem.revision = newer, 1
        db.add(Revision(problem_id=pid, number=1, document=newer, author_id=problem.owner_id, message="New"))
        db.get(Placement, (problem.owner_id, pid)).folder_id = destination
        db.commit()
    completed = finish(owner, batch["id"])
    assert completed["items"][0]["revision"] == 0
    unchanged = owner.get(f"/api/problems/{pid}").json()
    assert unchanged["version"] == response.json()["version"]
    assert unchanged["document"] == dirty
    with SessionLocal() as db:
        job = db.get(Job, completed["items"][0]["job_id"])
        assert job.revision == 0
        assert job.document["info"]["name"] == "문제 0"
        assert db.get(Problem, pid).revision == 1


def test_partial_zoj_export_reports_blockers_without_modifying_problem(owner, monkeypatch):
    monkeypatch.setattr(worker, "Pipeline", forbid_execution)
    root = folder(owner)
    good = add(owner, root, 1, ready=True)
    bad = add(owner, root, 2)
    with SessionLocal() as db:
        before = copy.deepcopy(db.get(Problem, bad).document)
    batch = start(owner, root, export_format="zoj")
    completed = finish(owner, batch["id"])
    assert (completed["status"], completed["ready"], completed["failed"]) == ("partial", 1, 1)
    failure = next(item for item in completed["items"] if item["problem_id"] == bad)
    assert failure["error"]["code"] == "package_tests_not_prepared" or failure["error"]["code"] == "zoj_incompatible"
    with zipfile.ZipFile(io.BytesIO(owner.get(completed["download_url"]).content)) as archive:
        assert len(archive.namelist()) == 2
        assert json.loads(archive.read("manifest.json"))["failed"] == 1
    with SessionLocal() as db:
        assert db.get(Problem, bad).document == before


def test_existing_archive_is_reused_across_backend_releases_but_not_verification_modes(owner):
    root = folder(owner)
    add(owner, root)
    first = finish(owner, start(owner, root)["id"])
    original = first["items"][0]["job_id"]
    with SessionLocal() as db:
        job = db.get(Job, original)
        job.options = {**job.options, "runtime_revision": "previous-editor-release"}
        db.commit()
    second = finish(owner, start(owner, root)["id"])
    assert second["items"][0]["job_id"] == original
    assert second["items"][0]["reused"] is True
    with SessionLocal() as db:
        job = db.get(Job, original)
        job.result = {**job.result, "package": {**job.result["package"], "verified": True}}
        db.commit()
    third = finish(owner, start(owner, root)["id"])
    assert third["items"][0]["job_id"] != original


def test_package_permission_is_required_for_cached_and_new_items_and_bundle(owner, clients):
    root = folder(owner)
    pid = add(owner, root)
    finish(owner, start(owner, root)["id"])
    member = clients("member")
    grant(owner, pid, "member", ["package"])
    personal = folder(member)
    assert member.post("/api/folders/move-problems", json={"problem_ids": [pid], "folder_id": personal}).status_code == 200
    batch = finish(member, start(member, personal)["id"])
    assert batch["items"][0]["reused"] is True
    grant(owner, pid, "member", [])
    response = member.get(batch["download_url"])
    assert response.status_code == 409
    detail = member.get(f"/api/folder-package-exports/{batch['id']}").json()
    assert detail["failed"] == 1 and detail["ready"] == 0
    denied = start(member, personal)
    assert denied["status"] == "failed"
    assert denied["items"][0]["error"]["code"] == "forbidden"
    assert member.get(f"/api/folder-package-exports/{batch['id']}").status_code == 200
    assert owner.get(f"/api/folder-package-exports/{batch['id']}").status_code == 404


def test_shared_root_revocation_and_active_batch_limit(owner, clients):
    member = clients("member")
    space = shared(owner)
    add_problem(owner, space)
    member_id = team(owner, member, space, ["package"])
    batch = start(member, space)
    owner.delete(f"/api/shared-folders/{space}/members/{member_id}")
    exports.advance_exports()
    assert member.get(f"/api/folder-package-exports/{batch['id']}").status_code == 404
    with SessionLocal() as db:
        row = db.get(FolderPackageExport, batch["id"])
        assert row.status == "failed"
        assert not db.scalars(select(Job)).all()
    root = folder(owner)
    add(owner, root, 4)
    start(owner, root)
    start(owner, root, include_descendants=False)
    response = owner.post(f"/api/folders/{root}/package-exports", json={"verify": True})
    assert response.status_code == 429


def test_verified_cache_uses_actual_agent_revisions_and_all_testsets(owner, monkeypatch):
    root = folder(owner)
    pid = add(owner, root, ready=True)
    with SessionLocal() as db:
        problem = db.get(Problem, pid)
        doc = copy.deepcopy(problem.document)
        doc["testsets"]["other"] = copy.deepcopy(doc["testsets"]["tests"])
        problem.document = doc
        db.get(Revision, (pid, 0)).document = copy.deepcopy(doc)
        from app.expectations import VERSION
        from app.reference_generation import PROTOCOL
        options = JobOptions(export_format="zoj", verify=True, testsets=list(doc["testsets"])).model_dump()
        options.update(snapshot="committed", source_document_hash=document_hash(doc), source_revision=0,
            execution_backend="zoj", execution_transport="agent", runtime_revision="old-backend-release",
            expectations_version=VERSION, reference_output_protocol=PROTOCOL,
            reference_revision="test-judge-v1", judge_revision="test-judge-v1", execution_revision="test-execution-v1")
        job = Job(problem_id=pid, user_id=problem.owner_id, kind="package", status="completed", document=doc,
            document_hash=document_hash(doc), revision=0, workspace_version=0, options=options)
        db.add(job)
        db.flush()
        artifact = build_package(job, {name: list(config["tests"].values()) for name, config in doc["testsets"].items()})
        job.result = {"verified": True, "package": artifact}
        db.commit()
        original = job.id
    batch = start(owner, root, verify=True, export_format="zoj")
    exports.advance_exports()
    detail = owner.get(f"/api/folder-package-exports/{batch['id']}").json()
    assert detail["ready"] == 1
    assert detail["items"][0]["job_id"] == original
    monkeypatch.setattr("app.judge_bridge.capabilities", lambda user: {"runtime_revision": "new-agent", "reference_output_protocol": PROTOCOL,
        "reference_output_online_nodes": 1, "execution": {"runtime_revision": "new-execution", "protocol": "editor-execute-v1", "online_nodes": 1}})
    next_batch = start(owner, root, verify=True, export_format="zoj")
    exports.advance_exports()
    detail = owner.get(f"/api/folder-package-exports/{next_batch['id']}").json()
    assert detail["items"][0]["job_id"] != original
    with SessionLocal() as db:
        job = db.get(Job, detail["items"][0]["job_id"])
        assert job.options["testsets"] == ["tests", "other"]
        assert job.options["execution_backend"] == "zoj"
        assert job.options["execution_transport"] == "agent"


def test_status_permission_checks_have_bounded_query_count(owner):
    root = folder(owner)
    for index in range(30):
        add(owner, root, index)
    batch = start(owner, root)
    exports.advance_exports()
    statements = []
    def collect(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", collect)
    try:
        response = owner.get(f"/api/folder-package-exports/{batch['id']}")
        assert response.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert len(statements) < 20
    assert not any("jobs.document" in statement for statement in statements)


def test_download_limits_bytes_before_hashing_and_releases_database(owner, monkeypatch):
    root = folder(owner)
    add(owner, root)
    batch = finish(owner, start(owner, root)["id"])
    settings = get_settings()
    monkeypatch.setattr(settings, "folder_export_max_archive_bytes", 1)
    real_digest = exports.hashlib.file_digest
    hashes = []
    def checked_digest(source, name):
        assert engine.pool.checkedout() == 0
        hashes.append(source.name)
        return real_digest(source, name)
    monkeypatch.setattr(exports.hashlib, "file_digest", checked_digest)
    assert owner.get(batch["download_url"]).status_code == 413
    assert not hashes
    monkeypatch.setattr(settings, "folder_export_max_archive_bytes", 8 * 1024 ** 3)
    real_copy = exports.shutil.copyfileobj
    def checked_copy(source, destination, length):
        assert engine.pool.checkedout() == 0
        return real_copy(source, destination, length)
    monkeypatch.setattr(exports.shutil, "copyfileobj", checked_copy)
    assert owner.get(batch["download_url"]).status_code == 200
    assert hashes


def test_access_loss_redacts_even_previously_failed_items(owner, clients):
    root = folder(owner)
    pid = add(owner, root)
    member = clients("member")
    grant(owner, pid, "member", ["package"])
    personal = folder(member)
    assert member.post("/api/folders/move-problems", json={"problem_ids": [pid], "folder_id": personal}).status_code == 200
    batch = finish(member, start(member, personal, export_format="zoj")["id"])
    assert batch["failed"] == 1
    assert owner.put(f"/api/problems/{pid}/accesses", json={"login": "member", "role": "NONE"}).status_code == 200
    response = member.get(f"/api/folder-package-exports/{batch['id']}")
    item = response.json()["items"][0]
    assert item["name"] == "접근할 수 없는 문제"
    assert item["folder_path"] == [] and item["archive_path"] == "" and item["slug"] == ""
    assert item["job_id"] is None and item["error"]["code"] == "not_found"


def test_terminated_batch_jobs_still_count_toward_folder_job_cap(owner):
    root = folder(owner)
    for index in range(3):
        add(owner, root, index)
    batch = start(owner, root)
    exports.advance_exports()
    exports.advance_exports()
    with SessionLocal() as db:
        db.get(FolderPackageExport, batch["id"]).status = "failed"
        db.commit()
    start(owner, root)
    exports.advance_exports()
    with SessionLocal() as db:
        assert len(db.scalars(select(Job).where(Job.status.in_(exports.ACTIVE))).all()) == 2


def test_drain_requested_during_admission_prevents_new_child(owner, monkeypatch):
    root = folder(owner)
    add(owner, root)
    batch = start(owner, root)
    draining = False
    def cache_then_drain(*args):
        nonlocal draining
        draining = True
        return None
    monkeypatch.setattr(exports, "cached_package", cache_then_drain)
    exports.advance_exports(draining=lambda: draining)
    with SessionLocal() as db:
        assert not db.scalars(select(Job)).all()
    assert owner.get(f"/api/folder-package-exports/{batch['id']}").json()["status"] == "queued"


def test_missing_artifact_is_regenerated_and_requests_require_auth(owner, client):
    root = folder(owner)
    add(owner, root)
    first = finish(owner, start(owner, root)["id"])
    original = first["items"][0]["job_id"]
    exports.package_path(original).unlink()
    next_batch = finish(owner, start(owner, root)["id"])
    assert next_batch["items"][0]["job_id"] != original
    assert client.get(f"/api/folders/{root}/package-exports").status_code == 401
    assert client.post(f"/api/folders/{root}/package-exports", json={}).status_code == 401
    assert client.get(next_batch["download_url"]).status_code == 401
    assert client.get(f"/api/folder-package-exports/{next_batch['id']}").status_code == 401


def test_same_size_tamper_is_excluded_from_partial_bundle(owner, clients):
    root = folder(owner)
    for index in range(2):
        add(owner, root, index)
    batch = finish(owner, start(owner, root)["id"])
    broken = batch["items"][0]
    path = exports.package_path(broken["job_id"])
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 255]) + original[1:])
    response = owner.get(batch["download_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "partial" and manifest["failed"] == 1 and manifest["ready"] == 1
        failed = next(item for item in manifest["items"] if item["problem_id"] == broken["problem_id"])
        assert failed["error"]["code"] == "package_artifact_corrupt"
        assert broken["archive_path"] not in archive.namelist()
        assert len(archive.namelist()) == 2
    other = clients("other")
    assert other.get(batch["download_url"]).status_code == 404
    next_batch = finish(owner, start(owner, root)["id"])
    repaired = next(item for item in next_batch["items"] if item["problem_id"] == broken["problem_id"])
    assert repaired["job_id"] != broken["job_id"]
    assert next_batch["ready"] == 2
    assert owner.get(next_batch["download_url"]).status_code == 200


def test_concurrent_requests_dedupe_and_coordinators_attach_atomically(owner):
    root = folder(owner)
    for index in range(3):
        add(owner, root, index)
    def request(_):
        response = owner.post(f"/api/folders/{root}/package-exports", json={"verify": False, "export_format": "editor"})
        assert response.status_code == 202, response.text
        return response.json()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(request, range(2)))
    assert ids[0] == ids[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: exports.advance_exports(), range(2)))
    with SessionLocal() as db:
        jobs = db.scalars(select(Job)).all()
        row = db.get(FolderPackageExport, ids[0])
        attached = {item["job_id"] for item in row.items if item["job_id"]}
        assert len(jobs) == 2
        assert attached == {job.id for job in jobs}


def test_child_creation_and_attachment_roll_back_together(owner, monkeypatch):
    root = folder(owner)
    add(owner, root)
    batch = start(owner, root)
    real_create = exports._create_job
    def interrupt_after_flush(*args, **kwargs):
        real_create(*args, **kwargs)
        raise HTTPException(409, {"code": "admission_interrupted", "message": "Interrupted"})
    monkeypatch.setattr(exports, "_create_job", interrupt_after_flush)
    exports.advance_exports()
    with SessionLocal() as db:
        assert not db.scalars(select(Job)).all()
        item = db.get(FolderPackageExport, batch["id"]).items[0]
        assert item["status"] == "failed" and item["job_id"] is None
        assert item["error"]["code"] == "admission_interrupted"


def test_interactive_and_folder_admission_share_global_limit(owner):
    root = folder(owner)
    pid = add(owner, root, ready=True)
    state = owner.get(f"/api/problems/{pid}").json()
    for _ in range(4):
        response = owner.post(f"/api/problems/{pid}/jobs", json={"kind": "package", "version": state["version"],
            "reuse": False, "options": {"verify": False}})
        assert response.status_code == 202
    batch = start(owner, root, export_format="linux")
    def interactive():
        return owner.post(f"/api/problems/{pid}/jobs", json={"kind": "package", "version": state["version"],
            "reuse": False, "options": {"verify": False}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduled = pool.submit(exports.advance_exports)
        manual = pool.submit(interactive)
        scheduled.result()
        assert manual.result() in {202, 429}
    with SessionLocal() as db:
        assert len(db.scalars(select(Job).where(Job.status.in_(exports.ACTIVE))).all()) == 5


def test_json_status_projections_compile_for_postgresql():
    query = select(Job.id, Job.result["package"], Job.result["summary"].as_string(),
        Job.result["progress"].as_float()).where(Job.options["folder_export_id"].as_string().is_not(None))
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "->>" in sql and "jobs.document" not in sql


def test_scheduler_rotates_beyond_twenty_active_batches(owner, clients, monkeypatch):
    batches = []
    for index in range(11):
        actor = owner if index == 0 else clients(f"actor{index}")
        root = folder(actor)
        add(actor, root)
        batches.extend([start(actor, root)["id"], start(actor, root, include_descendants=False)["id"]])
    # Keep every batch pending, as if each user's interactive jobs had filled
    # admission. The second pass must still visit the last two requests.
    monkeypatch.setattr(exports, "active_folder_jobs", lambda db, user_id: ["busy1", "busy2"])
    exports.advance_exports()
    with SessionLocal() as db:
        first = set(db.scalars(select(FolderPackageExport.id).where(FolderPackageExport.last_coordinated_at > 0)))
        assert len(first) == 20
    exports.advance_exports()
    with SessionLocal() as db:
        visited = set(db.scalars(select(FolderPackageExport.id).where(FolderPackageExport.last_coordinated_at > 0)))
        assert visited == set(batches)


def test_shared_export_user_locks_have_global_order(owner, clients, monkeypatch):
    member = clients("member")
    actors = [(owner.get("/api/auth/me").json(), owner), (member.get("/api/auth/me").json(), member)]
    actors.sort(key=lambda item: item[0]["id"])
    (root_account, root_actor), (requester, guest) = actors
    space = shared(root_actor)
    add_problem(root_actor, space)
    team(root_actor, guest, space, ["package"], handle=requester["handle"])
    locked = []
    real_lock = exports.lock_user
    def lock(db, user):
        locked.append(user.id)
        real_lock(db, user)
    monkeypatch.setattr(exports, "lock_user", lock)
    start(guest, space)
    assert locked == sorted([root_account["id"], requester["id"]])


def test_drain_stops_remaining_capability_requests(owner, clients, monkeypatch):
    for actor in (owner, clients("member")):
        root = folder(actor)
        add(actor, root)
        start(actor, root, verify=True)
    draining, calls = False, []
    def capabilities(user):
        nonlocal draining
        calls.append(user.id)
        draining = True
        return {}
    monkeypatch.setattr("app.judge_bridge.capabilities", capabilities)
    exports.advance_exports(draining=lambda: draining)
    assert len(calls) == 1
    with SessionLocal() as db:
        assert not db.scalars(select(Job)).all()


@pytest.mark.parametrize("background", [False, True])
def test_legacy_crlf_storage_uses_user_then_problem_lock_order(owner, background):
    from app.storage import put_blob
    root = folder(owner)
    pid = add(owner, root, ready=True)
    with SessionLocal() as db:
        problem = db.get(Problem, pid)
        doc = copy.deepcopy(problem.document)
        doc["testsets"]["tests"]["tests"]["1"]["input_sha"] = put_blob(db, pid, b"983 271\r\n")["sha256"]
        problem.document = doc
        db.get(Revision, (pid, 0)).document = copy.deepcopy(doc)
        db.commit()
    if background:
        start(owner, root)
    locks = []
    def collect(conn, cursor, statement, parameters, context, many):
        lowered = statement.lower()
        if lowered.startswith("update users"):
            locks.append("user")
        elif lowered.startswith("update problems"):
            locks.append("problem")
    event.listen(engine, "before_cursor_execute", collect)
    try:
        if background:
            exports.advance_exports()
        else:
            response = owner.post(f"/api/problems/{pid}/jobs", json={"kind": "package", "version": 0,
                "reuse": False, "options": {"verify": False}})
            assert response.status_code == 202, response.text
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert "problem" in locks
    assert locks.index("user") < locks.index("problem")
    with SessionLocal() as db:
        assert db.get(Problem, pid).document == doc


def test_stale_personal_placement_excludes_hidden_current_revision(owner, clients):
    private_root = folder(owner)
    hidden = add(owner, private_root)
    member = clients("member")
    grant(owner, hidden, "member", ["package"])
    personal = folder(member)
    visible = add(member, personal, 1)
    assert member.post("/api/folders/move-problems", json={"problem_ids": [hidden], "folder_id": personal}).status_code == 200
    assert owner.put(f"/api/problems/{hidden}/accesses", json={"login": "member", "role": "NONE"}).status_code == 200
    with SessionLocal() as db:
        problem = db.get(Problem, hidden)
        problem.revision = 987
        doc = copy.deepcopy(problem.document)
        doc["info"]["name"] = "Hidden new revision"
        problem.document = doc
        db.commit()
    batch = start(member, personal)
    assert batch["total"] == 1
    assert [item["problem_id"] for item in batch["items"]] == [visible]
    assert hidden not in json.dumps(batch)
    assert all(item["revision"] != 987 for item in batch["items"])
    # The visible read-only row remains an explicit package-permission error.
    grant(owner, hidden, "member", [])
    next_batch = start(member, personal, include_descendants=False)
    assert next_batch["total"] == 2
    denied = next(item for item in next_batch["items"] if item["problem_id"] == hidden)
    assert denied["error"]["code"] == "forbidden"
