"""Compatibility errors identify unsupported settings without changing them."""
import copy

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.schema import Document, SourceFile, empty_document
from app.storage import put_blob
from app.zoj_package import check_compatibility


EXTRA_NAMES = ["check-range.py", "check-order.py"]
GRADER_NAMES = ["grader.cpp", "grader-helper.cpp"]


def configure(doc, *, extra, grader):
    for name in EXTRA_NAMES:
        doc["files"][name] = SourceFile(
            name=name, language="python3", sha256="a" * 64, size=1,
        ).model_dump()
    for name in GRADER_NAMES:
        doc["files"][name] = SourceFile(
            name=name, compile_with_solution=grader,
            sha256="b" * 64, size=1,
        ).model_dump()
    doc["extra_validators"] = list(EXTRA_NAMES) if extra else []
    return Document.model_validate(doc).model_dump()


def assert_guidance(error, *, extra, grader):
    assert error["code"] == "zoj_incompatible"
    assert error["unsupported_extra_validators"] == (EXTRA_NAMES if extra else [])
    assert error["unsupported_solution_files"] == (GRADER_NAMES if grader else [])
    assert len(error["issues"]) == int(extra) + int(grader)
    assert error["message"] == " ".join(error["issues"])
    if extra:
        issue = next(issue for issue in error["issues"] if issue.startswith("추가 Validator("))
        assert all(name in issue for name in EXTRA_NAMES)
        assert "Validator > 추가 Validator" in issue
        assert "기본 Validator 코드에 통합" in issue
        assert "다시 검증" in issue
    if grader:
        issue = next(issue for issue in error["issues"] if issue.startswith("풀이 결합 파일("))
        assert all(name in issue for name in GRADER_NAMES)
        assert "파일·리소스 > 파일 편집 > 풀이와 함께 컴파일 (grader)" in issue
        assert "필수 grader라면 해제하지" in issue
        assert "독립 실행 가능한 풀이" in issue
    for issue in error["issues"]:
        assert "불필요한" in issue and "저장·커밋" in issue


@pytest.mark.parametrize("extra,grader", [(True, False), (False, True), (True, True)])
def test_unsupported_settings_have_independent_actionable_issues(extra, grader):
    doc = configure(empty_document("Compatibility fixture"), extra=extra, grader=grader)
    original = copy.deepcopy(doc)
    with pytest.raises(HTTPException) as caught:
        check_compatibility(doc, {"statement_language": "korean"})
    assert caught.value.status_code == 400
    assert_guidance(caught.value.detail, extra=extra, grader=grader)
    assert doc == original


def test_unrelated_issues_keep_their_content_and_empty_cause_arrays():
    doc = configure(empty_document("Other issues"), extra=False, grader=False)
    doc["checker"] = ""
    doc["info"]["interactive"] = True
    with pytest.raises(HTTPException) as caught:
        check_compatibility(doc, {"statement_language": "korean"})
    error = caught.value.detail
    assert error["code"] == "zoj_incompatible"
    assert error["unsupported_extra_validators"] == []
    assert error["unsupported_solution_files"] == []
    assert error["issues"] == [
        "ZOJ에서 사용할 checker를 선택하세요.",
        "ZOJ는 현재 표준 입출력의 비대화형 문제를 지원합니다.",
    ]
    assert error["message"] == " ".join(error["issues"])


def test_ordinary_files_and_basic_validator_remain_compatible():
    doc = configure(empty_document("Compatible fixture"), extra=False, grader=False)
    doc["validator"] = EXTRA_NAMES[0]
    assert check_compatibility(doc, {"statement_language": "korean"}) is None


def saved_fixture(owner, problem, *, extra, grader):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = configure(populate(db, row), extra=extra, grader=grader)
        for name in [*EXTRA_NAMES, *GRADER_NAMES]:
            blob = put_blob(db, problem, ("# Fixture " + name + "\n").encode())
            doc["files"][name].update(blob)
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    return owner.get(f"/api/problems/{problem}").json()


@pytest.mark.parametrize("endpoint", ["jobs", "transfer"])
@pytest.mark.parametrize("extra,grader", [(True, False), (False, True), (True, True)])
def test_package_request_returns_causes_without_queuing_or_changing_materials(owner, problem, endpoint, extra, grader):
    state = saved_fixture(owner, problem, extra=extra, grader=grader)
    with SessionLocal() as db:
        before_jobs = db.scalar(select(func.count()).select_from(Job))
    path = f"/api/problems/{problem}/jobs" if endpoint == "jobs" else f"/api/zoj-transfer/problems/{problem}/packages"
    payload = {"kind": "package", "version": state["version"], "options": {"export_format": "zoj"}} if endpoint == "jobs" else {}
    response = owner.post(path, json=payload)
    assert response.status_code == 400, response.text
    assert_guidance(response.json()["error"], extra=extra, grader=grader)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before_jobs
        assert db.get(Problem, problem).document == state["document"]
        assert db.get(Revision, (problem, 0)).document == state["document"]
    assert owner.get(f"/api/problems/{problem}").json()["document"] == state["document"]


@pytest.mark.parametrize("endpoint", ["jobs", "transfer"])
def test_private_configuration_is_not_disclosed_before_authorization(owner, problem, client, clients, endpoint):
    state = saved_fixture(owner, problem, extra=True, grader=True)
    stranger = clients("stranger")
    path = f"/api/problems/{problem}/jobs" if endpoint == "jobs" else f"/api/zoj-transfer/problems/{problem}/packages"
    payload = {"kind": "package", "version": state["version"], "options": {"export_format": "zoj"}} if endpoint == "jobs" else {}
    for caller, status in [(client, 401), (stranger, 404)]:
        response = caller.post(path, json=payload)
        assert response.status_code == status
        assert "unsupported_extra_validators" not in response.json()["error"]
        assert all(name not in response.text for name in [*EXTRA_NAMES, *GRADER_NAMES])
