import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.expectations import evaluate, expected_tag
from app.schema import Document, empty_document
from app.packages import descriptor


def fixture():
    doc = empty_document("Scoped verdicts")
    group = {"points_policy": "COMPLETE_GROUP", "feedback_policy": "COMPLETE", "dependencies": []}
    tests = {str(i): {"index": i, "command": f"gen {i}", "input_sha": None, "answer_sha": None,
                     "group": name, "points": 0, "description": "", "sample": False}
             for i, name in enumerate(["small", "medium", "large"], 1)}
    doc["testsets"]["tests"].update(tests=tests, groups_enabled=True, groups={name: group for name in ["small", "medium", "large"]})
    source = {"name": "small.cpp", "kind": "source", "language": "cpp17", "sha256": "a" * 64, "size": 1,
              "tag": "TL", "extra_tags": {}, "group_tags": {"tests": {"small": "OK", "medium": "OK", "large": "TL"}}}
    doc["solutions"] = {source["name"]: source}
    tests = list(tests.values())
    rows = [{"testset": "tests", "index": i, "verdict": verdict} for i, verdict in enumerate(["AC", "AC", "TLE"], 1)]
    return doc, source, {"tests": tests}, rows


def test_groups_override_global_default_without_requiring_tle_in_small_groups():
    doc, source, prepared, rows = fixture()
    result = evaluate(doc, source, prepared, rows)
    assert result["matched"] and result["expectations_complete"]
    assert [(scope["group"], scope["tag"], scope["matched"]) for scope in result["expectations"]] == [
        ("small", "OK", True), ("medium", "OK", True), ("large", "TL", True)]
    rows[-1]["verdict"] = "AC"
    assert not evaluate(doc, source, prepared, rows)["matched"]


def test_partial_group_execution_never_claims_full_expectation_verification():
    doc, source, prepared, rows = fixture()
    prepared["tests"] = [prepared["tests"][-1]]
    doc["testsets"]["tests"]["tests"]["4"] = {**prepared["tests"][0], "index": 4}
    rows = [{"testset": "tests", "index": 3, "verdict": "AC"}]
    result = evaluate(doc, source, prepared, rows)
    assert result["matched"] and not result["expectations_complete"]
    assert result["expectations"][0]["matched"] is None
    rows[0]["verdict"] = "JUDGE_ERROR"
    assert not evaluate(doc, source, prepared, rows)["matched"]


def test_testset_names_are_not_misread_as_legacy_group_names():
    source = {"tag": "OK", "extra_tags": {"samples": "TL"}}
    assert expected_tag(source, "tests", "samples", ["tests", "samples"])[0] == "OK"
    assert expected_tag(source, "samples", "", ["tests", "samples"])[0] == "TL"


def test_nr_group_can_be_skipped_without_fabricating_case_results():
    doc, source, prepared, rows = fixture()
    source["group_tags"]["tests"]["large"] = "NR"
    result = evaluate(doc, source, prepared, rows[:2])
    assert result["matched"] and result["expectations_complete"]
    assert result["expectations"][-1]["skipped"]
    assert result["expectations"][-1]["tested"] == 0


@pytest.mark.parametrize("groups", [{"missing": {"small": "OK"}}, {"tests": {"missing": "OK"}}, {"tests": {"small": "MA"}}])
def test_invalid_group_or_tag_references_are_rejected(groups):
    doc, source, _, _ = fixture()
    source["group_tags"] = groups
    with pytest.raises(ValidationError):
        Document.model_validate(doc)


def test_legacy_document_hash_shape_and_exported_scopes_are_preserved():
    doc, source, prepared, _ = fixture()
    parsed = Document.model_validate(doc).model_dump()
    assert parsed["solutions"][source["name"]]["group_tags"] == source["group_tags"]
    job = SimpleNamespace(document=doc, revision=1, problem_id="demo")
    xml = descriptor(job, prepared)
    assert b'group="small" tag="OK"' in xml
    old = copy.deepcopy(source)
    del old["group_tags"]
    from app.schema import Solution
    assert "group_tags" not in Solution.model_validate(old).model_dump()


def test_readiness_recognizes_scoped_wrong_verdicts_and_ignores_nr_solutions():
    from app.domain import cautions
    doc, source, _, _ = fixture()
    source['tag']='OK'
    assert 'NO_WRONG_SOLUTIONS' not in {c['code'] for c in cautions(doc)}
    source['tag']='NR'
    assert 'NO_WRONG_SOLUTIONS' in {c['code'] for c in cautions(doc)}
