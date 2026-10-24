import copy
import pytest
from fastapi import HTTPException
from app import worker
from app.db import Job, SessionLocal
from app.domain import parse_script
from app.job_errors import exception_message, readable_result
from app.review_report import build
from app.schema import empty_document


def test_worker_persists_readable_validation_and_api_returns_it(owner, problem, queue_legacy_job, monkeypatch):
    class InvalidPipeline:
        def __init__(self, *args):
            self.result = {"progress": 12, "compile": {"gen": "completed"}}

        def run(self):
            parse_script("gen 100")

    monkeypatch.setattr(worker, "Pipeline", InvalidPipeline)
    state = owner.get(f"/api/problems/{problem}").json()
    created = queue_legacy_job(owner, problem, {"kind": "prepare", "version": state["version"]}).json()
    worker.process_job(worker.claim_job())
    response = owner.get(f'/api/problems/{problem}/jobs/{created["id"]}').json()
    assert response["status"] == "failed"
    result = response["result"]
    assert result["error_code"] == "invalid_script"
    assert "1행" in result["error"] and "gen 100 > 1" in result["error"]
    assert "400:" not in result["error"] and "'message':" not in result["error"]
    assert result["progress"] == 12 and result["compile"] == {"gen": "completed"}
    with SessionLocal() as db:
        assert db.get(Job, created["id"]).result["error"] == result["error"]


def test_saved_legacy_errors_are_readable_without_rewriting_history(owner, problem, queue_legacy_job):
    state = owner.get(f"/api/problems/{problem}").json()
    created = queue_legacy_job(owner, problem, {"kind": "prepare", "version": state["version"]}).json()
    message = "생성 스크립트: 1행: 'gen' 인자를 확인하세요.\n다음 줄"
    raw = str(HTTPException(400, {"code": "invalid_script", "message": message}))
    with SessionLocal() as db:
        job = db.get(Job, created["id"])
        job.status = "failed"
        job.result = {"summary": "작업을 완료하지 못했습니다.", "error": raw, "progress": 3}
        db.commit()
    result = owner.get(f'/api/problems/{problem}/jobs/{created["id"]}').json()["result"]
    assert result["error"] == message and result["error_code"] == "invalid_script"
    assert result["progress"] == 3
    with SessionLocal() as db:
        assert db.get(Job, created["id"]).result["error"] == raw


@pytest.mark.parametrize("raw", ["compiler: unexpected token", "400: {broken", "400: {'message': 2}",
                                  "400: {'message': __import__('os').getcwd()}", "400: " + "[" * 1000])
def test_unstructured_logs_are_preserved_and_never_executed(raw):
    result = {"summary": "failed", "error": raw}
    original = copy.deepcopy(result)
    assert readable_result(result) == original
    assert result == original


def test_report_uses_validation_message_instead_of_dict():
    doc = empty_document("Invalid script")
    doc["testsets"]["tests"]["script"] = "gen 100"
    item = next(item for item in build(doc)["items"] if item["code"] == "INVALID_GENERATION_SCRIPT")
    assert "1행" in item["evidence"] and "gen 100 > 1" in item["evidence"]
    assert "'message':" not in item["evidence"]


def test_http_text_details_and_unexpected_errors_stay_readable():
    assert exception_message(HTTPException(400, "다시 확인하세요.")) == "다시 확인하세요."
    assert exception_message(RuntimeError("worker unavailable")) == "worker unavailable"
