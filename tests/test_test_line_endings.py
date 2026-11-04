import copy
import uuid
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import Blob, Problem, Revision, SessionLocal
from app.domain import document_hash
from app.packages import build_package, descriptor
from app.schema import Document, SourceFile, Test as CaseData, UnitTest
from app.storage import put_blob, read_blob
from app.test_line_endings import normalize_test_bytes, normalize_test_document
from test_feature_permissions import grant
from test_polygon_import import package, rewrite_package


def seed(problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = copy.deepcopy(row.document)
        raw = put_blob(db, problem, b"2\r\n7 8  ")
        answer = put_blob(db, problem, b"15\r\n")
        binary = put_blob(db, problem, b"\0\r\n")
        code = put_blob(db, problem, b"# source\r\npass\r\n")
        doc["files"]["validator.py"] = SourceFile(name="validator.py", language="python3", **code).model_dump()
        doc["files"]["raw.txt"] = SourceFile(name="raw.txt", language="text", kind="aux", **raw).model_dump()
        doc["validator"] = "validator.py"
        doc["testsets"]["tests"]["tests"] = {
            "1": CaseData(index=1, input_sha=raw["sha256"], answer_sha=answer["sha256"],
                          statement_input="2\r\n7 8  ", statement_output="15\r", sample=True).model_dump(),
            "2": CaseData(index=2, input_sha=binary["sha256"], answer_sha=binary["sha256"]).model_dump(),
        }
        doc["validator_tests"] = {
            "1": UnitTest(input_sha=raw["sha256"], expected="VALID").model_dump(),
            "2": UnitTest(input_sha=raw["sha256"], expected="INVALID", preserve_line_endings=True).model_dump(),
        }
        doc["checker_tests"] = {
            "1": UnitTest(input_sha=raw["sha256"], answer_sha=answer["sha256"], output_sha=answer["sha256"], expected="OK").model_dump(),
            "2": UnitTest(input_sha=raw["sha256"], answer_sha=answer["sha256"], output_sha=answer["sha256"],
                          expected="WRONG_ANSWER", preserve_line_endings=True).model_dump(),
        }
        row.document = Document.model_validate(doc).model_dump()
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
        return copy.deepcopy(row.document), raw["sha256"], answer["sha256"]


@pytest.mark.parametrize("content,expected", [
    (b"2\r\n7  8 ", b"2\n7  8 "), (b"2\r7 8\r", b"2\n7 8\n"),
    (b"", b""), (b"2\n7 8", b"2\n7 8"),
    (b"\xff\r\n", b"\xff\r\n"), (b"\0\r\n", b"\0\r\n"),
    ("\ufeff입력 🎲\r\n끝".encode(), "\ufeff입력 🎲\n끝".encode()),
])
def test_byte_policy_preserves_numbers_spacing_eof_and_binary(content, expected):
    assert normalize_test_bytes(content) == expected


def test_false_override_keeps_legacy_document_hash_and_true_roundtrips(owner, problem):
    doc, _, _ = seed(problem)
    assert "preserve_line_endings" not in doc["validator_tests"]["1"]
    expanded = copy.deepcopy(doc)
    expanded["validator_tests"]["1"]["preserve_line_endings"] = False
    assert document_hash(Document.model_validate(expanded).model_dump()) == document_hash(doc)
    assert Document.model_validate(doc).model_dump()["validator_tests"]["2"]["preserve_line_endings"] is True


def test_document_helper_clones_preserves_raw_overrides_sources_and_original_files(owner, problem):
    doc, raw, answer = seed(problem)
    before = copy.deepcopy(doc)
    with SessionLocal() as db:
        normalized, report = normalize_test_document(db, problem, doc)
        assert report == {"files": 2, "tests": 3}
        db.commit()
    assert doc == before
    row = normalized["testsets"]["tests"]["tests"]["1"]
    assert read_blob(row["input_sha"]) == b"2\n7 8  "
    assert read_blob(row["answer_sha"]) == b"15\n"
    assert (row["statement_input"], row["statement_output"]) == ("2\n7 8  ", "15\n")
    assert normalized["testsets"]["tests"]["tests"]["2"] == doc["testsets"]["tests"]["tests"]["2"]
    for area in ("validator_tests", "checker_tests"):
        assert normalized[area]["1"]["input_sha"] == row["input_sha"]
        assert normalized[area]["2"] == doc[area]["2"]
    assert normalized["files"] == doc["files"] and normalized["solutions"] == doc["solutions"]
    assert read_blob(raw) == b"2\r\n7 8  " and read_blob(answer) == b"15\r\n"
    assert read_blob(normalized["files"]["validator.py"]["sha256"]) == b"# source\r\npass\r\n"
    with SessionLocal() as db:
        assert db.get(Problem, problem).document == before


def test_helper_validates_every_selected_blob_before_conversion(owner, problem, monkeypatch):
    doc, _, _ = seed(problem)
    other = owner.post("/api/problems", json={"name": "Other", "slug": "other-format"}).json()["id"]
    foreign = owner.post(f"/api/problems/{other}/blobs", content=b"private\r\n").json()["sha256"]
    doc["validator_tests"]["1"]["input_sha"] = foreign
    def unexpected(*args):
        raise AssertionError("Foreign file scopes must be rejected before scanning")
    monkeypatch.setattr("app.test_line_endings.profile", unexpected)
    with SessionLocal() as db, pytest.raises(HTTPException) as error:
        normalize_test_document(db, problem, doc)
    assert error.value.detail["code"] == "invalid_blob"


def test_general_case_actual_output_fields_are_never_normalized(owner, problem):
    doc, raw, _ = seed(problem)
    case = doc["testsets"]["tests"]["tests"]["1"]
    case["actual_sha"] = case["output_sha"] = raw
    with SessionLocal() as db:
        normalized, _ = normalize_test_document(db, problem, doc, areas={"testsets"})
        db.commit()
    result = normalized["testsets"]["tests"]["tests"]["1"]
    assert result["actual_sha"] == result["output_sha"] == raw
    assert result["input_sha"] != raw


@pytest.mark.parametrize("scope,normal_tests,normal_units", [
    ("edit_info", False, False), ("edit_tests", True, False), ("edit_files", False, True),
])
def test_save_only_normalizes_the_actors_editable_areas(owner, clients, problem, scope, normal_tests, normal_units):
    original, raw, _ = seed(problem)
    editor = clients("limited")
    grant(owner, problem, "limited", [scope])
    state = editor.get(f"/api/problems/{problem}").json()
    response = editor.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert response.status_code == 200, response.text
    doc = response.json()["document"]
    assert response.json()["document_hash"] == document_hash(doc)
    assert (doc["testsets"]["tests"]["tests"]["1"]["input_sha"] != raw) == normal_tests
    assert (doc["validator_tests"]["1"]["input_sha"] != raw) == normal_units
    assert doc["files"] == original["files"]
    assert doc["validator_tests"]["2"] == original["validator_tests"]["2"]
    assert editor.get(f"/api/problems/{problem}").json()["document"] == doc


def test_unauthorized_input_edits_are_checked_before_automatic_normalization(owner, clients, problem):
    _, raw, _ = seed(problem)
    editor = clients("limited")
    grant(owner, problem, "limited", ["edit_info"])
    state = editor.get(f"/api/problems/{problem}").json()
    state["document"]["validator_tests"]["1"]["preserve_line_endings"] = True
    with SessionLocal() as db:
        before = set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem)))
    response = editor.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert response.status_code == 403
    with SessionLocal() as db:
        assert set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem))) == before
    assert read_blob(raw) == b"2\r\n7 8  "


def test_commit_normalizes_legacy_tests_but_preview_never_writes_blob_references(owner, problem):
    doc, raw, _ = seed(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    with SessionLocal() as db:
        before = set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem)))
    preview = owner.post(f"/api/problems/{problem}/commit-preview", json={"version": state["version"], "document": doc})
    assert preview.status_code == 200 and preview.json()["after"] == doc
    with SessionLocal() as db:
        assert set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem))) == before
    committed = owner.post(f"/api/problems/{problem}/commit", json={"version": state["version"], "message": "Normalize tests"})
    assert committed.status_code == 200 and committed.json()["committed"]
    current = owner.get(f"/api/problems/{problem}").json()
    assert current["document"]["validator_tests"]["1"]["input_sha"] != raw
    assert current["document"]["validator_tests"]["2"]["input_sha"] == raw
    assert owner.get(f"/api/problems/{problem}/revisions/1").json() == current["document"]


def test_resolving_legacy_workspaces_normalizes_only_the_saved_copy(owner, problem):
    original, raw, _ = seed(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    response = owner.post(f"/api/problems/{problem}/resolve", json={"version": state["version"], "revision": 0, "choices": {}})
    assert response.status_code == 200, response.text
    current = owner.get(f"/api/problems/{problem}").json()
    assert current["document"]["validator_tests"]["1"]["input_sha"] != raw
    assert current["document"]["validator_tests"]["2"]["input_sha"] == raw
    assert owner.get(f"/api/problems/{problem}/revisions/0").json() == original


@pytest.mark.parametrize("export_format", ["editor", "zoj"])
def test_native_and_zoj_packages_normalize_defaults_and_preserve_explicit_raw_unit_flags(owner, problem, export_format):
    doc, raw, _ = seed(problem)
    job = SimpleNamespace(id=uuid.uuid4().hex, document=doc, revision=0, problem_id=problem,
                          document_hash=document_hash(doc), options={"export_format": export_format, "verify": False,
                          "testset": "tests", "statement_language": "korean"})
    sets = {"tests": list(doc["testsets"]["tests"]["tests"].values())}
    artifact = build_package(job, sets)
    from app.config import get_settings
    content = (get_settings().data_dir / "packages" / (job.id + ".zip")).read_bytes()
    response = owner.post(f"/api/imports?name=Restored&slug=restored-{export_format}", content=content)
    assert response.status_code == 201, response.text
    restored = owner.get("/api/problems/" + response.json()["id"]).json()["document"]
    assert artifact["format"] == export_format
    assert read_blob(restored["validator_tests"]["1"]["input_sha"]) == b"2\n7 8  "
    for area in ("validator_tests", "checker_tests"):
        assert restored[area]["2"] == doc[area]["2"]
        assert read_blob(restored[area]["2"]["input_sha"]) == read_blob(raw)
    assert restored["files"] == doc["files"]


def test_polygon_always_normalizes_valid_text_and_keeps_marked_raw_self_tests(owner):
    _, files = package()
    root = ET.fromstring(files["problem.xml"])
    units = ET.SubElement(root.find("assets/validators/validator"), "testset")
    ET.SubElement(units, "input-path-pattern").text = "validator-tests/%02d"
    tests = ET.SubElement(units, "tests")
    ET.SubElement(tests, "test", verdict="valid")
    ET.SubElement(tests, "test", verdict="invalid", **{"preserve-line-endings": "true"})
    files["validator-tests/01"] = b"7\r\n8\r"
    files["validator-tests/02"] = b"7\r\n8\r"
    files["tests/02"] = b"8\r9  "
    files["problem.xml"] = ET.tostring(root)
    response = owner.post("/api/imports?name=Polygon&slug=polygon-normalize", content=rewrite_package(files))
    assert response.status_code == 201, response.text
    doc = owner.get("/api/problems/" + response.json()["id"]).json()["document"]
    assert read_blob(doc["validator_tests"]["1"]["input_sha"]) == b"7\n8\n"
    assert doc["validator_tests"]["2"]["preserve_line_endings"] is True
    assert read_blob(doc["validator_tests"]["2"]["input_sha"]) == b"7\r\n8\r"
    assert read_blob(doc["testsets"]["tests"]["tests"]["2"]["input_sha"]) == b"8\n9  "
    # Our Polygon-compatible descriptor carries the explicit override too.
    xml = descriptor(SimpleNamespace(document=doc, revision=0, problem_id="fixture"), {"tests": []})
    exported = ET.fromstring(xml).findall("assets/validator/testset/tests/test")
    assert exported[1].get("preserve-line-endings") == "true"
