import copy
import io
import json
import zipfile

import pytest

from app import worker
from app.config import get_settings
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.storage import read_blob
from app.storage import put_blob
from app.schema import SourceFile


def forbid_execution(*args, **kwargs):
    pytest.fail("Unverified export must not initialize a sandbox or contact an execution agent")


def run_export(owner, problem, monkeypatch, export_format="zoj"):
    monkeypatch.setattr(worker, "Pipeline", forbid_execution)
    monkeypatch.setattr("app.judge_bridge.capabilities", forbid_execution)
    monkeypatch.setattr(get_settings(), "execution_transport", "agent")
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={
        "kind": "package", "version": state["version"], "reuse": False,
        "options": {"verify": False, "export_format": export_format}})
    assert response.status_code == 202, response.text
    job = worker.claim_job()
    assert job.id == response.json()["id"]
    worker.process_job(job)
    result = owner.get(f"/api/problems/{problem}/jobs/{job.id}").json()
    assert result["status"] == "completed", result
    assert result["result"]["verified"] is False
    assert result["result"]["runtime"] == {"backend": "archive", "execution": "none"}
    assert not result["result"]["solutions"] and not result["result"]["unit_tests"]
    assert result["result"]["package"]["verified"] is False
    archive = owner.get(f"/api/problems/{problem}/packages/{job.id}")
    assert archive.status_code == 200
    return zipfile.ZipFile(io.BytesIO(archive.content)), archive.content


@pytest.mark.parametrize("export_format", ["zoj", "editor", "linux"])
def test_saved_files_export_without_any_code_execution(owner, problem, monkeypatch, export_format):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        # Keep deliberately incorrect component self-tests: no verification is
        # allowed to run or block a requested archive of the stored materials.
        doc = copy.deepcopy(row.document)
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    archive, content = run_export(owner, problem, monkeypatch, export_format)
    with archive:
        if export_format == "zoj":
            manifest = json.loads(archive.read("manifest.json"))
            snapshot = next(a for a in manifest['assets'] if a['original_filename'] == 'editor-project.json')
            assert json.loads(archive.read(snapshot['file']))['verified'] is False
            for source, test in zip(doc["testsets"]["tests"]["tests"].values(), manifest["testcase_sets"][0]["testcases"]):
                assert archive.read(test["input"]) == read_blob(source["input_sha"])
                assert archive.read(test["output"]) == read_blob(source["answer_sha"])
        else:
            assert json.loads(archive.read("editor.json"))["verified"] is False
            if export_format == "linux":
                environment = json.loads(archive.read("replay/environment.json"))
                assert environment["verified_at_export"] is False
                assert environment["measured_environment"] is None
    response = owner.post("/api/imports?name=Unverified&slug=unverified-" + export_format, content=content)
    assert response.status_code == 201, response.text
    restored = owner.get("/api/problems/" + response.json()["id"]).json()["document"]
    assert restored["solutions"] == doc["solutions"]
    assert restored["testsets"]["tests"]["tests"] == doc["testsets"]["tests"]["tests"]


def test_editor_source_archive_preserves_incomplete_draft(owner, problem, monkeypatch):
    code = b'print(1)\n'
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = copy.deepcopy(row.document)
        blob = put_blob(db, problem, code)
        doc['files']['unfinished.py'] = SourceFile(name='unfinished.py', language='python3', **blob).model_dump()
        doc['testsets']['tests']['script'] = 'unfinished.py 3 > $'
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    archive, content = run_export(owner, problem, monkeypatch, "editor")
    with archive:
        document = json.loads(archive.read("editor.json"))["document"]
        assert not document["solutions"] and not document["validator"]
        assert document['testsets']['tests']['script'] == 'unfinished.py 3 > $'
        assert archive.read('files/unfinished.py') == code
    response = owner.post("/api/imports?name=Draft&slug=restored-draft", content=content)
    assert response.status_code == 201, response.text
    assert owner.get("/api/problems/" + response.json()["id"]).json()["document"] == document


def test_saved_zoj_answers_do_not_require_main_or_validator_to_export(owner, problem, monkeypatch):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['solutions'] = {}
        doc['validator'] = None
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    archive, _ = run_export(owner, problem, monkeypatch)
    with archive:
        assert len(json.loads(archive.read('manifest.json'))['testcase_sets'][0]['testcases']) == 3


def test_unprepared_zoj_tests_are_reported_before_queueing(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc["testsets"]["tests"]["tests"]["2"]["answer_sha"] = None
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "package", "version": state["version"],
        "options": {"verify": False, "export_format": "zoj"}})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "package_tests_not_prepared"
    assert "tests #2" in response.json()["error"]["message"]
    with SessionLocal() as db:
        assert not db.query(Job).count()
