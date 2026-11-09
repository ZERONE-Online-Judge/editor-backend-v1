import copy
import json

import pytest
from sqlalchemy import event, select

from app.config import get_settings
from app.db import (Access, Folder, Problem, Revision, SessionLocal,
                    SharedPlacement, SharedWorkspace, User, WorkingCopy,
                    WorkspaceMember, engine)
from app.schema import empty_document
from test_shared_auth import central  # noqa: F401


@pytest.fixture
def master(owner, central):
    central["identity"]["is_service_master"] = True
    owner.cookies.set("zoj_sso", "central-refresh-token", domain="testserver.local", path="/")
    return owner


def user(db, name):
    row = User(email=f"{name}@example.com", handle=name, display_name=name.title())
    db.add(row)
    db.flush()
    return row


def problem(db, owner, name, *, created=100, deleted=False, number=None):
    doc = empty_document(name)
    doc["common_statement"] = "PRIVATE-STATEMENT-CONTENT"
    row = Problem(owner_id=owner.id, slug=name.lower().replace(" ", "-"),
                  document=doc, created_at=created, updated_at=created, deleted=deleted,
                  **({"id": f"{number:032x}"} if number is not None else {}))
    db.add(row)
    db.flush()
    db.add(Access(problem_id=row.id, user_id=owner.id, role="OWNER"))
    db.add(Revision(problem_id=row.id, number=0, author_id=owner.id, created_at=created,
                    message="PRIVATE-COMMIT-MESSAGE", document=copy.deepcopy(doc)))
    return row


def working(db, row, actor, *, version=1, at=1000, revision=0):
    draft = copy.deepcopy(row.document)
    draft["info"]["name"] = "PRIVATE-DRAFT-NAME"
    db.add(WorkingCopy(problem_id=row.id, user_id=actor.id, version=version,
        base_revision=revision, document=draft, base_document=copy.deepcopy(row.document), updated_at=at))


def test_master_capability_is_request_bound_strict_and_not_persisted(master, central):
    assert master.get("/api/auth/me").json()["is_service_master"] is True
    assert master.get("/api/service-master/problems").status_code == 200
    for value in (False, "true", 1, None):
        central["identity"]["is_service_master"] = value
        assert master.get("/api/auth/me").json()["is_service_master"] is False
        response = master.get("/api/service-master/problems")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "service_master_required"
    central["identity"].pop("is_service_master")
    assert master.get("/api/service-master/problems").status_code == 403
    central["identity"]["is_service_master"] = True
    assert master.get("/api/service-master/problems").status_code == 200
    assert "is_service_master" not in User.__table__.columns
    central["active"] = False
    assert master.get("/api/service-master/problems").status_code == 401


def test_local_auth_and_client_spoofing_cannot_obtain_master_access(owner):
    assert owner.get("/api/auth/me").json()["is_service_master"] is False
    assert owner.get("/api/service-master/problems?is_service_master=true",
                     headers={"X-Service-Master": "true"}).status_code == 403
    assert owner.get("/api/service-master/problems/" + "a" * 32).status_code == 403
    owner.cookies.clear()
    assert owner.get("/api/service-master/problems").status_code == 401


def test_profile_response_retains_only_the_current_trusted_capability(master, central):
    response = master.patch("/api/auth/profile", json={"display_name": "Updated", "handle": "updated"})
    assert response.status_code == 200
    assert response.json()["is_service_master"] is True
    assert response.json()["handle"] == "updated"
    central["identity"]["is_service_master"] = False
    response = master.patch("/api/auth/profile", json={"display_name": "Updated", "handle": "updated"})
    assert response.status_code == 200
    assert response.json()["is_service_master"] is False
    assert master.get("/api/service-master/problems").status_code == 403


def test_metadata_is_paged_searched_and_sorted_before_pagination(master):
    with SessionLocal() as db:
        creator = user(db, "writer")
        rows = [problem(db, creator, f"Question {n:02}", created=100, number=n)
                for n in range(1, 14)]
        hidden = problem(db, creator, "Removed", created=900, deleted=True, number=20)
        ids = [row.id for row in rows]
        removed = hidden.id
        db.commit()
    initial = master.get("/api/service-master/problems").json()
    assert initial["total"] == 13 and initial["page_size"] == 10 and initial["pages"] == 2
    assert [item["id"] for item in initial["items"]] == ids[:10]
    tail = master.get("/api/service-master/problems?page=999").json()
    assert tail["page"] == 2 and [item["id"] for item in tail["items"]] == ids[10:]
    for term in ("WRITER", "writer@example.com", "Question", "question-"):
        assert master.get("/api/service-master/problems", params={"q": term}).json()["total"] == 13
    assert master.get("/api/service-master/problems", params={"q": "Question 03"}).json()["items"][0]["id"] == ids[2]
    assert master.get("/api/service-master/problems", params={"q": "%"}).json()["total"] == 0
    for sort in ("recent", "created", "name", "owner"):
        value = master.get("/api/service-master/problems", params={"sort": sort, "page_size": 3, "page": 2}).json()
        assert value["total"] == 13 and [item["id"] for item in value["items"]] == ids[3:6]
    deleted = master.get("/api/service-master/problems?deleted=true").json()
    assert [item["id"] for item in deleted["items"]] == [removed]
    assert deleted["items"][0]["deleted"] is True
    assert master.get("/api/service-master/problems?q=no-match&page=3").json() == {
        "items": [], "total": 0, "page": 1, "page_size": 10, "pages": 1}


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 51},
    {"sort": "unknown"}, {"q": "x" * 201}, {"deleted": "unknown"}])
def test_invalid_directory_options_are_rejected(master, params):
    assert master.get("/api/service-master/problems", params=params).status_code == 422


def test_latest_activity_ignores_read_only_copies_and_favors_commit_on_ties(master):
    with SessionLocal() as db:
        creator, editor = user(db, "creator"), user(db, "editor")
        first = problem(db, creator, "Original", created=100)
        second = problem(db, creator, "Recent", created=200)
        untouched = problem(db, creator, "Untouched", created=300)
        working(db, first, editor, version=0, at=9999)
        working(db, second, editor, version=1, at=600)
        db.add(Revision(problem_id=first.id, number=1, author_id=creator.id,
            created_at=500, document=copy.deepcopy(first.document), message="PRIVATE-COMMIT-MESSAGE"))
        db.add(Revision(problem_id=second.id, number=1, author_id=creator.id,
            created_at=600, document=copy.deepcopy(second.document), message="PRIVATE-COMMIT-MESSAGE"))
        first.revision, second.revision = 1, 1
        ids, creator_id = [second.id, first.id, untouched.id], creator.id
        db.commit()
    items = master.get("/api/service-master/problems").json()["items"]
    assert [item["id"] for item in items] == ids
    assert [(item["last_activity_at"], item["last_activity_type"]) for item in items] == [
        (600, "commit"), (500, "commit"), (300, "created")]
    assert all(item["last_actor"]["id"] == creator_id for item in items)
    assert items[0]["name"] == "Recent"
    with SessionLocal() as db:
        copy_row = db.get(WorkingCopy, (ids[0], db.scalar(select(User.id).where(User.handle == "editor"))))
        copy_row.updated_at = 601
        db.commit()
    assert master.get("/api/service-master/problems").json()["items"][0]["last_activity_type"] == "working_copy"


def test_sort_columns_use_creator_title_creation_and_saved_activity(master):
    with SessionLocal() as db:
        author_a, author_z = user(db, "adam"), user(db, "zoe")
        alpha = problem(db, author_z, "Alpha", created=100, number=1)
        zulu = problem(db, author_a, "Zulu", created=200, number=2)
        working(db, alpha, author_z, version=1, at=300)
        db.commit()
    expected = {"recent": [1, 2], "created": [2, 1], "name": [1, 2], "owner": [2, 1]}
    for sort, numbers in expected.items():
        response = master.get("/api/service-master/problems", params={"sort": sort})
        assert [item["id"] for item in response.json()["items"]] == [f"{number:032x}" for number in numbers]


def test_current_editors_include_custom_translation_and_shared_grants(master):
    with SessionLocal() as db:
        creator, custom, translator, viewer, revoked, folder_owner, member = [user(db, name)
            for name in ("creator", "custom", "translator", "viewer", "revoked", "folderowner", "member")]
        row = problem(db, creator, "Shared")
        space = SharedWorkspace(owner_id=folder_owner.id)
        db.add(space)
        db.flush()
        folder = Folder(id=space.id, user_id=folder_owner.id, workspace_id=space.id,
                        parent_key="", name="Team", name_key="team")
        db.add(folder)
        db.flush()
        db.add(SharedPlacement(problem_id=row.id, folder_id=folder.id))
        db.add(WorkspaceMember(workspace_id=space.id, user_id=member.id, role="EDITOR", permissions={"edit_tests": True}))
        db.add_all([Access(problem_id=row.id, user_id=custom.id, role="READ", permissions={"edit_info": True}),
            Access(problem_id=row.id, user_id=translator.id, role="READ", translator=True),
            Access(problem_id=row.id, user_id=viewer.id, role="WRITE", permissions={})])
        working(db, row, revoked, version=3, at=1000)
        working(db, row, member, version=2, at=700, revision=0)
        working(db, row, viewer, version=0, at=9999)
        ident, member_id, space_id = row.id, member.id, space.id
        expected = {actor.id for actor in (creator, custom, translator, folder_owner, member)}
        db.commit()
    detail = master.get(f"/api/service-master/problems/{ident}").json()
    assert detail["problem"]["editor_count"] == len(expected)
    assert {item["user"]["id"] for item in detail["editors"]} == expected
    assert detail["editors"][0]["role"] == "OWNER"
    current = next(item for item in detail["editors"] if item["user"]["id"] == member_id)
    assert current["last_saved_at"] == 700 and current["working_revision"] == 0
    assert detail["last_commit"]["number"] == 0
    with SessionLocal() as db:
        db.delete(db.get(WorkspaceMember, (space_id, member_id)))
        db.commit()
    after = master.get(f"/api/service-master/problems/{ident}").json()
    assert member_id not in {item["user"]["id"] for item in after["editors"]}
    assert after["problem"]["editor_count"] == len(expected) - 1


def test_directory_is_read_only_and_does_not_grant_problem_or_file_access(master):
    with SessionLocal() as db:
        creator = user(db, "unshared")
        row = problem(db, creator, "Confidential")
        working(db, row, creator)
        ident = row.id
        db.commit()
    def snapshot():
        with SessionLocal() as db:
            return {model.__tablename__: [tuple(getattr(row, col.name) for col in model.__table__.columns)
                    for row in db.scalars(select(model))]
                    for model in (Problem, WorkingCopy, Revision, Access, SharedPlacement, WorkspaceMember)}
    before = snapshot()
    statements = []
    def track(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", track)
    try:
        response = master.get(f"/api/service-master/problems/{ident}")
        listing = master.get("/api/service-master/problems")
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert response.status_code == listing.status_code == 200
    for value in (response.json(), listing.json()):
        serialized = json.dumps(value)
        assert "PRIVATE-" not in serialized
        def safe(node):
            if isinstance(node, dict):
                assert not set(node) & {"document", "base_document", "message", "sha256", "input_sha", "answer_sha", "solutions", "testsets", "statements"}
                for child in node.values():
                    safe(child)
            elif isinstance(node, list):
                for child in node:
                    safe(child)
        safe(value)
    # JSON extraction of only the public title is allowed; full draft/history
    # document projections would be both unnecessary and prone to data leaks.
    assert not any("working_copies.document" in sql or "working_copies.base_document" in sql or "revisions.document" in sql for sql in statements)
    assert before == snapshot()
    assert master.get(f"/api/problems/{ident}").status_code == 404
    assert master.put(f"/api/problems/{ident}/working-copy", json={"version": 0, "document": empty_document("Spoof")}).status_code == 404
    assert master.get(f"/api/problems/{ident}/blobs/" + "a" * 64).status_code == 404
    assert before == snapshot()
    assert master.get("/api/service-master/problems/" + "f" * 32).status_code == 404


def test_directory_query_count_does_not_grow_per_problem(master):
    with SessionLocal() as db:
        creator = user(db, "bulk")
        for n in range(25):
            problem(db, creator, f"Bulk {n:02}")
        db.commit()
    def count(size):
        statements = []
        def track(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", track)
        try:
            response = master.get("/api/service-master/problems", params={"page_size": size})
        finally:
            event.remove(engine, "before_cursor_execute", track)
        assert response.status_code == 200, response.text
        return len(statements)
    count(1)  # Link the central profile before comparing read counts.
    assert count(1) == count(25)
