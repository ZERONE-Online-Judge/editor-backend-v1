"""Account tags resolve to the same account-bound invitation as email."""
import pytest
from sqlalchemy import select
from app.db import AccessMail, SessionLocal, User, WorkspaceInvitation
from test_shared_folders import shared, add_problem


@pytest.mark.parametrize("identifier", [
    "@editor-0123456789", "editor-0123456789", "  @editor-0123456789  ",
    "MEMBER@EXAMPLE.COM", " member@example.com ",
])
def test_tag_and_email_invites_share_recipient_mail_and_acceptance(owner, clients, identifier):
    member = clients("member")
    outsider = clients("outsider")
    with SessionLocal() as db:
        db.scalar(select(User).where(User.handle == "member")).handle = "editor-0123456789"
        db.commit()
    space = shared(owner)
    pid = add_problem(owner, space)
    endpoint = f"/api/shared-folders/{space}/invitations"
    result = owner.post(endpoint, json={"login": identifier, "permissions": ["execute", "issues"]})
    assert result.status_code == 201, result.text
    ident = result.json()["id"]
    with SessionLocal() as db:
        assert db.get(WorkspaceInvitation, ident).email == "member@example.com"
        mail = db.scalars(select(AccessMail)).all()
        assert len(mail) == 1 and mail[0].recipient_email == "member@example.com"
    # Email and tag must not create separate invitations or duplicate mail.
    repeat = owner.post(endpoint, json={"email": "member@example.com", "permissions": ["issues", "execute"]})
    assert repeat.status_code == 201 and repeat.json()["id"] == ident
    with SessionLocal() as db:
        assert len(db.scalars(select(AccessMail)).all()) == 1
    assert member.get(f"/api/problems/{pid}").status_code == 404
    assert outsider.post(f"/api/shared-folders/invitations/{ident}/accept").status_code == 404
    assert member.post(f"/api/shared-folders/invitations/{ident}/accept").status_code == 200
    record = member.get(f"/api/problems/{pid}").json()["problem"]
    assert set(record["permissions"]) == {"execute", "issues"}
    assert member.post(endpoint, json={"login": "@outsider"}).status_code == 403
    assert owner.post(endpoint, json={"login": "@editor-0123456789"}).status_code == 409


def test_unknown_tag_never_creates_invitation_or_mail(owner):
    space = shared(owner)
    result = owner.post(f"/api/shared-folders/{space}/invitations", json={"login": "@editor-unknown"})
    assert result.status_code == 404 and result.json()["error"]["code"] == "user_not_found"
    with SessionLocal() as db:
        assert db.scalars(select(WorkspaceInvitation)).all() == []
        assert db.scalars(select(AccessMail)).all() == []


@pytest.mark.parametrize("payload", [
    {}, {"login": "   "}, {"login": "@@editor-abc"}, {"login": "not-an@email"},
    {"email": "@editor-abc"}, {"login": "@member", "email": "other@example.com"},
    {"login": None, "email": None},
])
def test_invalid_or_ambiguous_recipients_are_rejected(owner, payload):
    space = shared(owner)
    assert owner.post(f"/api/shared-folders/{space}/invitations", json=payload).status_code == 422


def test_email_invite_still_works_before_account_exists(owner, clients):
    space = shared(owner)
    result = owner.post(f"/api/shared-folders/{space}/invitations", json={"login": "FUTURE@example.com", "permissions": []})
    assert result.status_code == 201, result.text
    future = clients("future")
    assert future.post(f'/api/shared-folders/invitations/{result.json()["id"]}/accept').status_code == 200


def test_tag_invites_obey_delegated_permission_limits(owner, clients):
    from test_feature_permissions import team
    manager = clients("manager")
    clients("member")
    space = shared(owner)
    team(owner, manager, space, ["manage_access", "issues"], handle="manager")
    endpoint = f"/api/shared-folders/{space}/invitations"
    assert manager.post(endpoint, json={"login": "@member", "permissions": ["publish"]}).status_code == 403
    assert manager.post(endpoint, json={"login": "@member", "permissions": ["issues"]}).status_code == 201


def test_problem_collaboration_also_accepts_displayed_at_tag(owner, clients, problem):
    member = clients("member")
    result = owner.put(f"/api/problems/{problem}/accesses", json={"login": "  @member  ", "permissions": ["issues"]})
    assert result.status_code == 200, result.text
    assert member.get(f"/api/problems/{problem}").json()["problem"]["permissions"] == ["issues"]
