"""Legacy duplicate selections are interpreted once, never rewritten."""
import copy
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.pipeline import Pipeline
from app.sandbox import SandboxError
from app.schema import SourceFile, empty_document
from app.storage import read_blob
from app.validators import validator_names
from app.zoj_package import build_zoj_package, check_compatibility


@pytest.mark.parametrize("basic,extra,expected", [
    ("validator.cpp", [], ["validator.cpp"]),
    ("validator.cpp", ["validator.cpp", "validator.cpp"], ["validator.cpp"]),
    ("validator.cpp", ["second.cpp", "validator.cpp", "third.cpp", "second.cpp"],
     ["validator.cpp", "second.cpp", "third.cpp"]),
    ("", ["second.cpp", "second.cpp"], ["second.cpp"]),
    ("", [], []),
])
def test_validator_names_preserve_first_selection_order_and_document(basic, extra, expected):
    document = {"validator": basic, "extra_validators": extra}
    original = copy.deepcopy(document)
    assert validator_names(document) == expected
    assert document == original


def recording_pipeline(basic, extra, *, reject=None):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.doc = {"validator": basic, "extra_validators": extra}
    pipeline.program = lambda name: name
    calls = []
    def run(program, content, **options):
        calls.append((program, content, options))
        return {"status": "ok", "exit_code": int(program == reject), "stderr": program}
    pipeline.sandbox = SimpleNamespace(run=run)
    return pipeline, calls


def test_pipeline_executes_each_validator_once_with_the_same_input_and_scope():
    pipeline, calls = recording_pipeline("validator.cpp", ["second.cpp", "validator.cpp", "second.cpp", "third.cpp"])
    original = copy.deepcopy(pipeline.doc)
    valid, result = pipeline.validator(b"2 3\n", "samples", "small")
    assert valid and result["stderr"] == "third.cpp"
    assert [name for name, _, _ in calls] == ["validator.cpp", "second.cpp", "third.cpp"]
    assert all(content == b"2 3\n" and options == {
        "args": ["--testset", "samples", "--group", "small"], "cpu_ms": 10000, "memory_mb": 512,
    } for _, content, options in calls)
    assert pipeline.doc == original


def test_real_extra_validator_rejection_still_stops_execution():
    pipeline, calls = recording_pipeline("validator.cpp", ["validator.cpp", "second.cpp", "third.cpp"], reject="second.cpp")
    valid, result = pipeline.validator(b"2 3\n")
    assert not valid and result["stderr"] == "second.cpp"
    assert [name for name, _, _ in calls] == ["validator.cpp", "second.cpp"]


def test_extra_validator_does_not_replace_missing_basic_selection():
    pipeline, calls = recording_pipeline("", ["second.cpp"])
    with pytest.raises(SandboxError, match="validator를 선택하세요"):
        pipeline.validator(b"2 3\n")
    assert calls == []


@pytest.mark.parametrize("basic,extra,unsupported", [
    ("validator.cpp", ["validator.cpp"], []),
    ("validator.cpp", ["second.cpp", "validator.cpp", "third.cpp", "second.cpp"], ["second.cpp", "third.cpp"]),
    ("", ["second.cpp"], ["second.cpp"]),
])
def test_zoj_compatibility_reports_only_real_extra_validators(basic, extra, unsupported):
    document = empty_document("Validator selection fixture")
    document.update(validator=basic, extra_validators=extra)
    for name in {basic, *extra} - {""}:
        document["files"][name] = SourceFile(name=name, sha256="a" * 64, size=1).model_dump()
    original = copy.deepcopy(document)
    if unsupported:
        with pytest.raises(HTTPException) as caught:
            check_compatibility(document, {"statement_language": "korean"})
        error = caught.value.detail
        assert error["code"] == "zoj_incompatible"
        assert error["unsupported_extra_validators"] == unsupported
        assert len(error["issues"]) == 1
        assert error["issues"][0].startswith(f"추가 Validator({', '.join(unsupported)})")
    else:
        assert check_compatibility(document, {"statement_language": "korean"}) is None
    assert document == original


@pytest.mark.parametrize("endpoint", ["jobs", "transfer"])
def test_committed_duplicate_can_queue_and_export_without_rewriting_source(owner, problem, endpoint):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        document = populate(db, row)
        document["extra_validators"] = [document["validator"], document["validator"]]
        original = copy.deepcopy(document)
        row.document = copy.deepcopy(document)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(document)
        db.commit()
    state = owner.get(f"/api/problems/{problem}").json()
    path = f"/api/problems/{problem}/jobs" if endpoint == "jobs" else f"/api/zoj-transfer/problems/{problem}/packages"
    payload = {"kind": "package", "version": state["version"], "options": {"export_format": "zoj"}} if endpoint == "jobs" else {}
    response = owner.post(path, json=payload)
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        job = db.get(Job, response.json()["id"])
        assert job.document == original
        job_document = copy.deepcopy(job.document)
        build_zoj_package(job, {"tests": list(document["testsets"]["tests"]["tests"].values())})
        assert job.document == job_document
        assert db.get(Problem, problem).document == original
        assert db.get(Revision, (problem, 0)).document == original
        validator_bytes = read_blob(original["files"][original["validator"]]["sha256"])
        archive_path = get_settings().data_dir / "packages" / (job.id + ".zip")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        validators = [asset for asset in manifest["assets"] if asset["category"] == "package-files/validator"]
        assert len(validators) == 1
        assert validators[0]["original_filename"] == original["validator"]
        assert archive.read(validators[0]["file"]) == validator_bytes
        snapshot_asset = next(asset for asset in manifest["assets"] if asset["original_filename"] == "editor-project.json")
        snapshot = json.loads(archive.read(snapshot_asset["file"]))
        assert snapshot["source_document"] == original
        assert snapshot["document"]["validator"] == original["validator"]
        assert snapshot["document"]["extra_validators"] == original["extra_validators"]
    assert owner.get(f"/api/problems/{problem}").json()["document"] == original
