"""Master bridge authentication, durable pagination and source-free evidence."""
import base64
import copy
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import JSON, cast, event, null, select

from app.admin_job_history import PURPOSE
from app.config import get_settings
from app.db import Job, LibraryNonce, Problem, SessionLocal, User, engine, now

KEY = "admin-history-test-secret-" + "x" * 40
PATH = "/api/internal/admin/jobs"


def headers(target=PATH, *, purpose=PURPOSE, stamp=None, nonce=None, identity="master@example.com", method="GET", body=b""):
    stamp, nonce = str(now() if stamp is None else stamp), nonce or uuid4().hex
    encoded = base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([purpose, method, target, stamp, nonce, encoded, digest])
    return {"X-Editor-Identity": encoded, "X-Editor-Timestamp": stamp, "X-Editor-Nonce": nonce,
            "X-Editor-Content-Sha256": digest,
            "X-Editor-Signature": hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "zoj_bridge_secret", SecretStr(KEY))


def seed(owner, problem, count=6):
    document = owner.get(f"/api/problems/{problem}").json()["document"]
    document["common_statement"] = "PRIVATE-STATEMENT"
    result = {"summary": "검증 결과를 확인하세요.", "progress": 67, "verified": False,
              "verification_progress": {"current": 2, "total": 3, "percent": 67, "phase": "judging"},
              "solutions": [{"name": "solve.cpp", "tag": "WA", "matched": False, "state": "completed", "total_tests": 4,
                             "cases": [{"testset": "tests", "index": 1, "verdict": "AC", "cpu_ms": 1, "memory_kb": 1024,
                                        "input_sha": "PRIVATE-HASH", "actual_sha": "PRIVATE-ACTUAL-HASH", "output": "PRIVATE-OUTPUT"},
                                       {"testset": "tests", "index": 2, "verdict": "WA", "cpu_ms": 8, "memory_kb": 2048,
                                        "stderr": "PRIVATE-STDERR", "comment": "PRIVATE-COMMENT"},
                                       {"testset": "tests", "index": 3, "verdict": "JUDGE_ERROR"}]}],
              "error": "PRIVATE-ERROR /srv/secret token=PRIVATE-TOKEN",
              "package": {"path": "/srv/PRIVATE-PACKAGE", "token": "PRIVATE-ARTIFACT-TOKEN"},
              "events": [{"label": "검증 중 token=PRIVATE-TOKEN /srv/secret", "source": "PRIVATE-SOURCE"}]}
    with SessionLocal() as db:
        author = db.get(Problem, problem).owner_id
        for index in range(count):
            db.add(Job(id=f"{index + 1:032x}", problem_id=problem, user_id=author,
                kind="verify" if index % 2 == 0 else "stress", status=["queued", "running", "completed"][index % 3],
                document=copy.deepcopy(document), document_hash="d" * 64, workspace_version=4, revision=2,
                created_at=100, started_at=101, finished_at=102 if index % 3 == 2 else None,
                options={"name": f"실행 {index}", "single_verification": index == 0, "verify": True,
                         "temporary_solutions": [{"content": "PRIVATE-SOURCE"}], "lease": "PRIVATE-LEASE",
                         "source_document_hash": "PRIVATE-DOCUMENT-HASH", "note": "PRIVATE-NOTE"}, result=copy.deepcopy(result)))
        db.commit()
    return result


def test_an_editor_browser_session_does_not_grant_bridge_access(owner, problem):
    seed(owner, problem, 1)
    assert owner.get(PATH).status_code == 401
    assert owner.get(PATH + "/" + f"{1:032x}", headers={"X-Service-Master": "true"}).status_code == 401


def test_missing_bridge_configuration_is_explicitly_unavailable(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "zoj_bridge_secret", None)
    value = client.get(PATH, headers=headers())
    assert value.status_code == 503 and value.json()["error"]["code"] == "admin_history_disabled"


def test_signed_server_get_needs_neither_a_browser_cookie_nor_csrf_header():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as server:
        response = server.get(PATH, headers=headers())
    assert response.status_code == 200 and response.json()["items"] == []


@pytest.mark.parametrize("options", [{"purpose": "zoj-editor-v1"}, {"stamp": 0}, {"stamp": now() + 1000},
    {"nonce": "bad"}, {"identity": "not-email"}, {"method": "POST"}, {"body": b"content"}])
def test_signature_scope_identity_freshness_and_empty_body_are_required(client, options):
    assert client.get(PATH, headers=headers(**options)).status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(LibraryNonce)) is None


def test_signature_binds_exact_query_order_values_and_detail_path(client):
    signed = PATH + "?page=1&page_size=20"
    for target in (PATH + "?page=2&page_size=20", PATH + "?page_size=20&page=1", PATH + "/" + "a" * 32):
        assert client.get(target, headers=headers(signed)).status_code == 401
    assert client.get(signed, headers=headers(signed)).status_code == 200


def test_signature_cannot_authorize_a_get_request_with_a_body(client):
    assert client.request("GET", PATH, headers=headers(), content=b"unbound").status_code == 401


def test_nonce_is_consumed_once_even_for_parallel_requests(client):
    signed = headers()
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: client.get(PATH, headers=signed).status_code, range(2)))
    assert sorted(statuses) == [200, 401]


def test_expired_nonces_are_removed_without_changing_job_evidence(owner, problem):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        db.add(LibraryNonce(nonce="e" * 32, expires_at=now() - 1))
        before = copy.deepcopy(db.get(Job, f"{1:032x}").result)
        db.commit()
    assert owner.get(PATH, headers=headers()).status_code == 200
    assert owner.get(PATH + "/" + f"{1:032x}", headers=headers(PATH + "/" + f"{1:032x}")).status_code == 200
    with SessionLocal() as db:
        assert db.get(LibraryNonce, "e" * 32) is None
        assert db.get(Job, f"{1:032x}").result == before
        assert db.get(Job, f"{1:032x}").status == "queued"


def test_durable_jobs_are_globally_paged_with_stable_ties_and_filter_totals(owner, problem):
    seed(owner, problem, 25)
    paths = [PATH + f"?page={page}&page_size=10" for page in (1, 2, 3, 999)]
    pages = [owner.get(path, headers=headers(path)).json() for path in paths]
    assert [page["page"] for page in pages] == [1, 2, 3, 3]
    assert [len(page["items"]) for page in pages] == [10, 10, 5, 5]
    assert all(page["total"] == 25 and page["pages"] == 3 for page in pages)
    assert [item["id"] for page in pages[:3] for item in page["items"]] == [f"{i:032x}" for i in range(25, 0, -1)]
    assert pages[0]["summary"] == {"total": 25, "queued": 9, "running": 8, "completed": 8, "failed": 0, "cancelled": 0}
    value = pages[0]["items"][0]
    assert value["name"] == "실행 24" and value["revision"] == 2 and value["workspace_version"] == 4
    assert value["requester"]["email"] == "author@example.com"
    assert value["problem"]["id"] == problem and value["problem"]["name"] == "두 수의 합"
    assert value["result"] == {"summary": "검증 결과를 확인하세요.", "progress": 67}
    assert "options" not in value and "document" not in value and "PRIVATE-" not in json.dumps(pages)
    completed = next(item for item in pages[0]["items"] if item["kind"] == "verify" and item["status"] == "completed")
    assert completed["status"] == "completed" and completed["outcome"]["assessment"]["code"] == "detail_required"


@pytest.mark.parametrize("suffix,total", [("kind=verify", 3), ("category=verify", 2), ("category=verify-single", 1),
    ("status=running", 2), ("q=%EC%8B%A4%ED%96%89+3", 1), ("q=AUTHOR%40EXAMPLE.COM", 6),
    ("q=%EB%91%90+%EC%88%98", 6), ("q=a-plus-b", 6), ("q=%25", 0), ("q=%5F", 0), ("kind=stress&status=completed", 1)])
def test_search_kind_category_status_are_applied_before_count_and_page(owner, problem, suffix, total):
    seed(owner, problem)
    target = PATH + "?page_size=1&" + suffix
    value = owner.get(target, headers=headers(target))
    assert value.status_code == 200, value.text
    assert value.json()["total"] == total and value.json()["summary"]["total"] == total
    assert len(value.json()["items"]) == min(total, 1)
    if suffix.startswith("category="):
        assert value.json()["items"][0]["category"] == suffix.removeprefix("category=")


@pytest.mark.parametrize("suffix", ["page=0", "page_size=0", "page_size=101", "status=unknown", "kind=unknown",
    "category=unknown", "q=" + "x" * 201])
def test_invalid_filters_are_rejected(client, suffix):
    target = PATH + "?" + suffix
    assert client.get(target, headers=headers(target)).status_code == 422


def test_detail_keeps_verdicts_and_progress_but_excludes_private_snapshots_diagnostics_and_artifacts(owner, problem):
    seed(owner, problem, 1)
    path = PATH + "/" + f"{1:032x}"
    response = owner.get(path, headers=headers(path))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["result_redacted"] is True and value["result_truncated"] is False
    assert value["options"] == {"name": "실행 0", "verify": True, "single_verification": True}
    assert value["result"]["has_error"] is True
    assert value["result"]["verification_progress"] == {"current": 2, "total": 3, "percent": 67, "phase": "judging"}
    result = value["result"]["solutions"][0]
    assert result["observed_verdicts"] == ["AC", "JUDGE_ERROR", "WA"]
    assert (result["passed_tests"], result["failed_tests"], result["unconfirmed_tests"]) == (1, 1, 2)
    assert result["max_cpu_ms"] == 8 and result["max_memory_kb"] == 2048
    assert len(result["cases"]) == 3
    assert "PRIVATE-" not in response.text and "/srv/" not in response.text
    assert "document" not in value and "lease" not in value and "package" not in value["result"]


def test_compile_failure_is_not_a_completed_test_and_large_case_lists_are_explicitly_capped(owner, problem):
    seed(owner, problem, 1)
    path = PATH + "/" + f"{1:032x}"
    with SessionLocal() as db:
        row = db.get(Job, f"{1:032x}")
        row.status = "completed"
        row.result = {"summary": "완료", "solutions": [{"name": "java.java", "tag": "OK", "total_tests": 1000,
            "cases": [{"index": 0, "verdict": "CE"}]}],
            "stress_results": [{"name": "many.cpp", "cases": [{"index": i + 1, "verdict": "AC", "cpu_ms": i} for i in range(1000)]}]}
        db.commit()
    response = owner.get(path, headers=headers(path))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["result_truncated"] is True
    compiled = value["result"]["solutions"][0]
    assert compiled["observed_verdicts"] == ["CE"] and compiled["passed_tests"] == compiled["failed_tests"] == 0
    assert compiled["unconfirmed_tests"] == 1000
    stress = value["result"]["stress_results"][0]
    assert stress["passed_tests"] == 1000 and len(stress["cases"]) == 500 and stress["max_cpu_ms"] == 999


def test_projection_budget_does_not_make_options_unserializable(owner, problem):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        row = db.get(Job, f"{1:032x}")
        row.result = {"solutions": [{"name": f"{i}.cpp", "cases": [{"index": j, "verdict": "AC", "cpu_ms": 1} for j in range(500)]} for i in range(30)]}
        db.commit()
    path = PATH + "/" + f"{1:032x}"
    response = owner.get(path, headers=headers(path))
    assert response.status_code == 200, response.text
    assert response.json()["result_truncated"] is True and response.json()["options"]["name"] == "실행 0"


def test_deleted_problems_remain_in_durable_history_and_missing_jobs_are_404(owner, problem):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        db.get(Problem, problem).deleted = True
        db.commit()
    assert owner.get(PATH, headers=headers()).json()["items"][0]["problem"]["deleted"] is True
    missing = PATH + "/" + "f" * 32
    assert owner.get(missing, headers=headers(missing)).status_code == 404


def test_list_does_not_fetch_a_job_document_or_create_a_personal_working_copy(owner, problem):
    seed(owner, problem, 3)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = owner.get(PATH, headers=headers())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert not any("jobs.document," in statement or "jobs.document \n" in statement for statement in selects)
    assert not any("INSERT INTO working_copies" in statement for statement in statements)


def test_completed_verified_flag_alone_cannot_claim_a_pass_on_the_lightweight_list(owner, problem):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        job = db.get(Job, f"{1:032x}")
        job.status = "completed"
        job.result = {"summary": "legacy verified flag", "verified": True, "solutions": [{"name": "a.cpp", "cases": []}]}
        db.commit()
    value = owner.get(PATH, headers=headers()).json()["items"][0]
    assert value["outcome"]["assessment"]["code"] == "detail_required"
    path = PATH + "/" + value["id"]
    assert owner.get(path, headers=headers(path)).json()["outcome"]["assessment"]["status"] != "passed"


def test_the_listing_selects_json_fragments_instead_of_full_evidence_or_options():
    from app.admin_job_history import query_jobs
    columns = set(query_jobs().selected_columns.keys())
    assert "result_summary" in columns and "result_progress" in columns
    assert "result" not in columns and "options" not in columns and "document" not in columns
    detail_columns = set(query_jobs(detail=True).selected_columns.keys())
    assert "result" in detail_columns and "options" in detail_columns and "document" not in detail_columns


def test_listing_pages_before_json_projection_and_counts_only_small_metadata(owner, problem):
    seed(owner, problem, 24)
    statements = []
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = owner.get(PATH + "?page=2&page_size=20", headers=headers(PATH + "?page=2&page_size=20"))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    value = response.json()
    assert (value["total"], value["page"], value["pages"]) == (24, 2, 2)
    assert [item["id"] for item in value["items"]] == [f"{i:032x}" for i in [4, 3, 2, 1]]
    aggregate = next(statement for statement in statements if "count(" in statement.lower())
    assert all(source not in aggregate for source in ("jobs.options", "jobs.result", "problems.document", "json_to_record"))
    page = next(statement for statement in statements if "admin_history_page AS" in statement)
    assert "LIMIT" in page and "OFFSET" in page
    if engine.dialect.name == "postgresql":
        assert "admin_history_page AS MATERIALIZED" in page
        assert page.count("json_to_record(") == 2
    else:
        assert "json_to_record(" not in page


@pytest.mark.parametrize("value", [None, [], [1, 2], True, False, "malformed structure", 17])
def test_listing_treats_nonobject_json_sources_as_empty_objects(owner, problem, value):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        job = db.get(Job, f"{1:032x}")
        job.status = "completed"
        job.options = value
        job.result = value
        db.commit()
    response = owner.get(PATH, headers=headers())
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["name"] == "" and item["category"] == "verify"
    assert item["result"] == {"summary": "", "progress": 0}
    assert item["outcome"]["assessment"]["code"] == "detail_required"


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="PostgreSQL JSON record expansion")
def test_postgresql_record_projection_accepts_a_sql_null_source():
    from app.admin_job_history import json_record
    record = json_record(cast(null(), JSON), ("name", "verify"), {"verify"}, "empty_record")
    with SessionLocal() as db:
        assert db.execute(select(record.c.name, record.c.verify)).one() == (None, None)


def test_stress_count_summary_is_exact_without_reading_case_payloads(owner, problem):
    seed(owner, problem, 2)
    with SessionLocal() as db:
        job = db.get(Job, f"{2:032x}")
        job.status = "completed"
        job.result = {"summary": "반례 발견", "stress": {"iterations_completed": 45, "counterexample_count": 2},
                      "uncovered_candidates": ["not-covered.cpp"],
                      "counterexamples": [{"input": "PRIVATE-INPUT"}], "stress_results": [{"cases": [{"output": "PRIVATE-OUTPUT"}]}]}
        db.commit()
    path = PATH + "?kind=stress"
    value = owner.get(path, headers=headers(path)).json()["items"][0]
    assert value["outcome"]["assessment"] == {"kind": "stress", "status": "found", "code": "counterexample_found",
        "partial": False, "iterations": 45, "found": 2, "uncovered": 1}


def test_legacy_stress_without_small_count_evidence_is_not_reported_as_counterexample_free(owner, problem):
    seed(owner, problem, 2)
    with SessionLocal() as db:
        job = db.get(Job, f"{2:032x}")
        job.status = "completed"
        job.result = {"iterations": 10, "counterexamples": [{"input": "PRIVATE-INPUT"}]}
        db.commit()
    value = owner.get(PATH, headers=headers()).json()["items"][0]
    assert value["outcome"]["assessment"]["code"] == "detail_required"


@pytest.mark.parametrize("options,result,code", [
    ({"verify": False}, {}, "verification_skipped"),
    ({"verify": True}, {"verification_skipped": True}, "verification_skipped"),
    ({"verify": None}, {"verification_skipped": None}, "detail_required"),
    ({}, {}, "detail_required"),
])
def test_package_list_preserves_false_and_null_verification_options(owner, problem, options, result, code):
    seed(owner, problem, 1)
    with SessionLocal() as db:
        job = db.get(Job, f"{1:032x}")
        job.kind, job.status, job.options, job.result = "package", "completed", options, result
        db.commit()
    value = owner.get(PATH, headers=headers()).json()["items"][0]
    assert value["outcome"]["assessment"]["code"] == code
    assert value["outcome"]["assessment"]["status"] == ("not_run" if code == "verification_skipped" else "unknown")
