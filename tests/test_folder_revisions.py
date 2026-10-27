import copy

from sqlalchemy import select

from app.db import SessionLocal, User, WorkingCopy
from app.revision_history import PREVIEW_BYTES, changes
from test_shared_folders import add_problem, join, shared


def commit(owner, pid, edit, message="파일 수정"):
    state = owner.get(f"/api/problems/{pid}").json()
    edit(state["document"])
    saved = owner.put(f"/api/problems/{pid}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert saved.status_code == 200, saved.text
    response = owner.post(f"/api/problems/{pid}/commit", json={"version": saved.json()["version"], "message": message})
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def file(owner, pid, content, name="main.cpp"):
    blob = owner.post(f"/api/problems/{pid}/blobs", content=content).json()
    return {"name": name, "kind": "source", "language": "cpp17", **blob}


def preview(client, pid, start=0, end=1):
    return client.get(f"/api/problems/{pid}/revision-preview?from_revision={start}&to_revision={end}")


def test_folder_collects_existing_child_commits_and_keeps_drafts_private(owner, clients):
    space = shared(owner)
    child = owner.post('/api/folders', json={"name": "예선", "parent_id": space}).json()["id"]
    pid = add_problem(owner, child)
    other = add_problem(owner, space, "other-task")
    commit(owner, pid, lambda d: d["info"].update(name="커밋한 제목"), "제목 정리")
    draft = owner.get(f"/api/problems/{pid}").json()
    draft["document"]["info"]["name"] = "비공개 작업 사본"
    owner.put(f"/api/problems/{pid}/working-copy", json={"version": draft["version"], "document": draft["document"]})
    viewer = clients("viewer")
    join(owner, viewer, space, "VIEWER", "viewer")
    response = viewer.get(f"/api/folders/{space}/revisions")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 3 and data["page_size"] == 10
    row = next(r for r in data["items"] if r["number"] == 1)
    assert row["problem_name"] == "커밋한 제목" and row["author"] == "author"
    assert row["folder_path"] == ["대회 공동 작업", "예선"]
    assert row["changes"]["items"] == [{"path": "info/name", "label": "이름", "kind": "changed"}]
    assert "비공개 작업 사본" not in response.text
    children = viewer.get(f"/api/folders/{child}/revisions").json()
    assert children["total"] == 2 and {r["problem_id"] for r in children["items"]} == {pid}
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.handle == "viewer"))
        assert db.get(WorkingCopy, (pid, user.id)) is None


def test_folder_access_revocation_movement_and_deleted_problems(owner, clients, problem):
    space = shared(owner)
    pid = add_problem(owner, space)
    outsider = clients("outsider")
    # Direct access to a problem does not grant team history access.
    owner.put(f"/api/problems/{pid}/accesses", json={"login": "outsider", "role": "READ"})
    assert outsider.get(f"/api/folders/{space}/revisions").status_code == 404
    join(owner, outsider, space, "VIEWER", "outsider")
    assert outsider.get(f"/api/folders/{space}/revisions").json()["total"] == 1
    owner.post('/api/folders/move-problems', json={"problem_ids": [pid], "folder_id": None, "share_confirmed": True})
    assert outsider.get(f"/api/folders/{space}/revisions").json()["total"] == 0
    owner.post('/api/folders/move-problems', json={"problem_ids": [problem], "folder_id": space, "share_confirmed": True})
    owner.patch(f'/api/problems/{problem}/deleted', json={"deleted": True})
    assert outsider.get(f"/api/folders/{space}/revisions").json()["total"] == 0
    member = next(r for r in owner.get(f"/api/shared-folders/{space}/members").json()["members"] if r["handle"] == "outsider")
    owner.delete(f"/api/shared-folders/{space}/members/{member['user_id']}")
    assert outsider.get(f"/api/folders/{space}/revisions").status_code == 404


def test_folder_history_has_bounded_deterministic_pages_and_rejects_invalid_sizes(owner, client):
    space = shared(owner)
    pid = add_problem(owner, space)
    for i in range(12):
        commit(owner, pid, lambda d, i=i: d["info"].update(name=f"제목 {i}"))
    first = owner.get(f"/api/folders/{space}/revisions").json()
    second = owner.get(f"/api/folders/{space}/revisions?page=2").json()
    assert first["total"] == 13 and len(first["items"]) == 10 and second["pages"] == 2
    assert [r["number"] for r in first["items"] + second["items"]] == list(range(12, -1, -1))
    for suffix in ("page=0", "page_size=11", "page_size=0"):
        assert owner.get(f"/api/folders/{space}/revisions?{suffix}").status_code == 422
    assert client.get(f"/api/folders/{space}/revisions").status_code == 401
    personal = owner.post('/api/folders', json={"name": "개인"}).json()["id"]
    assert owner.get(f"/api/folders/{personal}/revisions").status_code == 404


def test_preview_contains_commit_messages_real_source_diff_and_no_personal_draft(owner, problem, clients):
    old = file(owner, problem, b"int main() {\n  return 1;\n}\n")
    commit(owner, problem, lambda d: d["files"].update({"main.cpp": old}), "풀이 추가")
    new = file(owner, problem, b"int main() {\n  return 0;\n}\n")
    commit(owner, problem, lambda d: d["files"].update({"main.cpp": new}), "정답 수정")
    result = preview(owner, problem, 1, 2)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["commits"]["items"][0]["message"] == "정답 수정"
    change = data["changes"]["items"][0]
    assert change["path"] == "files/main.cpp" and change["kind"] == "changed"
    assert {line["text"] for line in change["lines"] if line["kind"] != "same"} >= {"-  return 1;", "+  return 0;"}
    assert not change["truncated"] and result.headers["cache-control"] == "no-store"
    assert clients("outsider").get(f"/api/problems/{problem}/revision-preview?from_revision=1").status_code == 404
    state = owner.get(f"/api/problems/{problem}").json()
    state["document"]["statements"]["korean"]["legend"] = "아직 공유하지 않은 지문"
    owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert "아직 공유하지 않은 지문" not in preview(owner, problem, 0, 2).text
    owner.patch(f"/api/problems/{problem}/deleted", json={"deleted": True})
    assert preview(owner, problem).status_code == 404


def test_preview_handles_add_remove_binary_long_files_and_truncation(owner, problem):
    source = file(owner, problem, b"+++ actual source line\n--- actual source line\n" + b"a\n" * 1000)
    binary = file(owner, problem, b"\0\x01\x02", "data.bin")
    huge = file(owner, problem, ("한글" * PREVIEW_BYTES).encode(), "long.txt")
    commit(owner, problem, lambda d: d["files"].update({"main.cpp": source, "data.bin": binary, "long.txt": huge}))
    added = preview(owner, problem).json()["changes"]["items"]
    assert len(added) == 3
    assert next(c for c in added if c["label"] == "data.bin")["binary"]
    assert next(c for c in added if c["label"] == "long.txt")["truncated"]
    main = next(c for c in added if c["label"] == "main.cpp")
    assert "+--- actual source line" in [line["text"] for line in main["lines"]]
    assert main["truncated"] and len(main["lines"]) <= 9
    commit(owner, problem, lambda d: d["files"].pop("main.cpp"))
    assert preview(owner, problem, 1, 2).json()["changes"]["items"][0]["kind"] == "removed"


def test_initial_and_cumulative_preview_are_bounded_and_validate_ranges(owner, problem):
    initial = preview(owner, problem, -1, 0).json()
    assert initial["commits"]["items"][0]["number"] == 0
    for i in range(7):
        commit(owner, problem, lambda d, i=i: d["info"].update(name=f"변경 {i}"), f"커밋 {i}")
    data = preview(owner, problem, 0, 7).json()
    assert data["commits"]["total"] == 7 and len(data["commits"]["items"]) == 5
    assert data["changes"]["total"] == 1 and "변경 6" in str(data["changes"])
    assert preview(owner, problem, 7, 0).status_code == 400
    assert preview(owner, problem, 0, 999).status_code == 404
    assert preview(owner, problem, 0, 2 ** 80).status_code == 404
    assert preview(owner, problem, -2, 1).status_code == 422
    assert preview(owner, problem, 7, 7).json()["changes"]["total"] == 0


def test_test_changes_use_test_numbers_instead_of_internal_keys():
    before = {"testsets": {"tests": {"script": "", "tests": {"internal-key": {"index": 2, "input_sha": "a"}}}}}
    after = copy.deepcopy(before)
    after["testsets"]["tests"]["tests"]["internal-key"]["input_sha"] = "b"
    row = changes(before, after)[0]
    assert row["path"] == "testsets/tests/tests/2" and row["label"] == "tests #2"
