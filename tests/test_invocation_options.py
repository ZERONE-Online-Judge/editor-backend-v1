import copy
import pytest
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate


def ready(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
    return owner.get(f"/api/problems/{problem}").json()


def test_selected_scope_and_metadata_are_validated_and_saved(owner, problem):
    state = ready(owner, problem)
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": state["version"], "options": {
        "name": "  경계 검수  ", "note": "일부 테스트 검증", "solutions": ["wrong.py", "wrong.py"], "test_indices": [3, 1, 3],
    }})
    assert response.status_code == 202, response.text
    options = response.json()["options"]
    assert options["name"] == "경계 검수" and options["note"] == "일부 테스트 검증"
    assert options["solutions"] == ["wrong.py"] and options["test_indices"] == [1, 3]
    assert options["scope"] == {"testset": "tests", "solutions": ["wrong.py"], "test_indices": [1, 3], "test_count": 2}


@pytest.mark.parametrize("indices,code", [([4], 400), ([0], 422), ([1001], 422), ([True], 422), ([1.5], 422)])
def test_invalid_indices_are_not_silently_ignored(owner, problem, indices, code):
    state = ready(owner, problem)
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": state["version"], "options": {"test_indices": indices}})
    assert response.status_code == code, response.text


def test_empty_selected_testset_does_not_report_success(owner, problem):
    state = ready(owner, problem)
    state["document"]["testsets"]["empty"] = {"tests": {}, "script": "", "groups": {}, "groups_enabled": False}
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": saved.json()["version"], "options": {"testset": "empty"}})
    assert response.status_code == 400 and response.json()["error"]["code"] == "empty_scope"


def test_committed_snapshot_does_not_save_or_require_current_copy_version(owner, problem):
    state = ready(owner, problem)
    state["document"]["info"]["name"] = "private author changes"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    response = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "verify", "version": state["version"], "revision": 0, "options": {"snapshot": "committed"}})
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        assert db.get(Job, response.json()["id"]).document["info"]["name"] != "private author changes"
    current = owner.get(f"/api/problems/{problem}").json()
    assert current["version"] == saved.json()["version"] and current["document"]["info"]["name"] == "private author changes"


def test_forced_preparation_does_not_return_a_previous_job(owner, problem):
    state = ready(owner, problem)
    request = {"kind":"verify", "version":state["version"], "options":{"force_prepare":True}}
    first = owner.post(f"/api/problems/{problem}/jobs", json=request)
    second = owner.post(f"/api/problems/{problem}/jobs", json=request)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
