import hashlib
import hmac
from threading import Event, Thread
import httpx
from pydantic import SecretStr
from sqlalchemy import select
from app import access_mail, mail_relay
from app.config import get_settings
from app.db import AccessMail, SessionLocal, WorkspaceInvitation, now
from test_shared_folders import shared


def rows():
    with SessionLocal() as db:
        return db.scalars(select(AccessMail).order_by(AccessMail.created_at, AccessMail.id)).all()


def test_direct_access_emails_new_read_access_and_added_scopes_only(owner, clients, problem, monkeypatch):
    clients('member')
    endpoint = f'/api/problems/{problem}/accesses'
    put = lambda **kw: owner.put(endpoint, json={'login': 'member', **kw})
    assert put(permissions=[]).status_code == 200
    assert len(rows()) == 1 and rows()[0].recipient_email == 'member@example.com'
    assert put(permissions=[]).status_code == 200
    assert len(rows()) == 1
    calls = []
    monkeypatch.setattr(mail_relay, 'send_access', lambda email, payload: calls.append((email, payload)) or 'receipt')
    assert access_mail.dispatch_one()
    assert rows()[0].status == 'queued'
    assert calls[0][1]['resource_name'] == '두 수의 합'
    assert calls[0][1]['permissions'] == []
    assert put(permissions=['issues']).status_code == 200
    assert len(rows()) == 2
    assert put(permissions=[]).status_code == 200
    assert put(role='NONE').status_code == 200
    assert len(rows()) == 2
    assert access_mail.dispatch_one()  # revoked before delivery
    assert len(calls) == 1
    assert {r.status for r in rows()} == {'queued', 'cancelled'}


def test_pending_invitation_and_permission_change_mail_before_account_exists(owner, monkeypatch):
    space = shared(owner)
    endpoint = f'/api/shared-folders/{space}/invitations'
    invite = owner.post(endpoint, json={'email':'NEW@example.com', 'permissions':[]})
    assert invite.status_code == 201, invite.text
    ident = invite.json()['id']
    assert owner.post(endpoint, json={'email':'new@example.com', 'permissions':[]}).status_code == 201
    assert len(rows()) == 1
    sent = []
    monkeypatch.setattr(mail_relay, 'send_access', lambda email, payload: sent.append((email, payload)) or 'receipt')
    assert access_mail.dispatch_one()
    assert sent[0][0] == 'new@example.com'
    assert sent[0][1]['kind'] == 'folder_invitation' and sent[0][1]['expires_at'] > now()
    assert owner.put(endpoint + '/' + ident, json={'permissions':['execute']}).status_code == 200
    assert len(rows()) == 2
    assert owner.put(endpoint + '/' + ident, json={'permissions':['execute']}).status_code == 200
    assert len(rows()) == 2
    assert owner.delete(endpoint + '/' + ident).status_code == 200
    assert access_mail.dispatch_one()
    assert len(sent) == 1


def test_member_added_permissions_notify_but_unchanged_or_removed_do_not(owner, clients, monkeypatch):
    member = clients('member'); space = shared(owner)
    invite = owner.post(f'/api/shared-folders/{space}/invitations', json={'email':'member@example.com'}).json()
    assert member.post(f'/api/shared-folders/invitations/{invite["id"]}/accept').status_code == 200
    ident = member.get('/api/auth/me').json()['id']
    endpoint = f'/api/shared-folders/{space}/members/{ident}'
    assert owner.put(endpoint, json={'permissions':['execute','issues']}).status_code == 200
    assert owner.put(endpoint, json={'permissions':['issues','execute']}).status_code == 200
    assert len(rows()) == 2
    calls = []
    monkeypatch.setattr(mail_relay, 'send_access', lambda email, payload: calls.append(payload) or 'receipt')
    while access_mail.dispatch_one():
        pass
    assert len(calls) == 1 and calls[0]['kind'] == 'folder_access'
    assert owner.put(endpoint, json={'permissions':[]}).status_code == 200
    assert len(rows()) == 2


def test_failed_or_unauthorized_changes_cannot_enqueue_mail(owner, clients, problem):
    outsider = clients('outsider')
    endpoint = f'/api/problems/{problem}/accesses'
    assert outsider.put(endpoint, json={'login':'author','permissions':[]}).status_code == 404
    assert owner.put(endpoint, json={'login':'missing','permissions':[]}).status_code == 404
    assert owner.put(endpoint, json={'login':'outsider','permissions':['manage_folders']}).status_code == 400
    assert rows() == []
    # An exception/rollback after staging a grant also rolls back its mail.
    with SessionLocal() as db:
        from app.db import User
        actor = db.scalar(select(User).where(User.handle == 'author'))
        access_mail.enqueue(db, kind='problem_access', resource_id=problem, resource_name='x',
            email='outsider@example.com', actor=actor, permissions=[])
        db.flush(); db.rollback()
    assert rows() == []


def test_retry_keeps_event_id_and_payload_and_recovers_after_worker_loss(owner, clients, problem, monkeypatch):
    clients('member')
    owner.put(f'/api/problems/{problem}/accesses', json={'login':'member','permissions':['execute']})
    calls = []
    def fail(email, payload):
        calls.append(payload)
        raise httpx.ReadTimeout('private response must not be stored')
    monkeypatch.setattr(mail_relay, 'send_access', fail)
    assert access_mail.dispatch_one()
    row = rows()[0]
    assert row.status == 'pending' and row.attempts == 1 and row.last_error == 'ReadTimeout'
    assert not access_mail.dispatch_one()
    with SessionLocal() as db:
        pending = db.get(AccessMail, row.id)
        pending.available_at = now() - 1
        pending.status = 'sending'  # a crashed worker's expired lease
        pending.lease = 'expired'
        db.commit()
    monkeypatch.setattr(mail_relay, 'send_access', lambda email, payload: calls.append(payload) or 'saved-receipt')
    assert access_mail.dispatch_one()
    assert calls[0] == calls[1]
    assert rows()[0].status == 'queued' and rows()[0].delivery_id == 'saved-receipt'


def test_two_workers_do_not_send_the_same_event_while_lease_is_active(owner, clients, problem, monkeypatch):
    clients('member')
    owner.put(f'/api/problems/{problem}/accesses', json={'login':'member','permissions':[]})
    started, release = Event(), Event()
    def send(email, payload):
        started.set()
        assert release.wait(5)
        return 'receipt'
    monkeypatch.setattr(mail_relay, 'send_access', send)
    worker = Thread(target=access_mail.dispatch_one)
    worker.start()
    try:
        assert started.wait(5)
        assert not access_mail.dispatch_one()
    finally:
        release.set(); worker.join(5)
    assert not worker.is_alive() and rows()[0].attempts == 1


def test_expired_invitation_is_cancelled_and_a_new_invite_notifies_again(owner, monkeypatch):
    space = shared(owner)
    endpoint = f'/api/shared-folders/{space}/invitations'
    invite = owner.post(endpoint, json={'email':'new@example.com'}).json()
    with SessionLocal() as db:
        db.get(WorkspaceInvitation, invite['id']).expires_at = now() - 1
        db.commit()
    monkeypatch.setattr(mail_relay, 'send_access', lambda *_: (_ for _ in ()).throw(AssertionError('expired mail')))
    assert access_mail.dispatch_one() and rows()[0].status == 'cancelled'
    assert owner.post(endpoint, json={'email':'new@example.com'}).status_code == 201
    assert len(rows()) == 2


def test_notification_uses_private_auth_transport_without_browser_credentials(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, 'auth_mode', 'zoj')
    monkeypatch.setattr(s, 'auth_api_url', 'https://10.10.10.110:6443/api')
    monkeypatch.setattr(s, 'auth_bridge_secret', SecretStr('auth-bridge-test-key-at-least-32-characters'))
    real_client = httpx.Client
    def receive(request):
        assert str(request.url) == 'https://10.10.10.110:6443/api/editor/mail/access'
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.headers['X-Editor-Identity'] == 'bWVtYmVyQGV4YW1wbGUuY29t'
        sha = hashlib.sha256(request.content).hexdigest()
        canonical = '\n'.join(['zoj-editor-access-mail-v1', 'POST', request.url.path,
            request.headers['X-Editor-Timestamp'], request.headers['X-Editor-Nonce'], request.headers['X-Editor-Identity'], sha])
        assert request.headers['X-Editor-Signature'] == hmac.new(s.auth_bridge_secret.get_secret_value().encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return httpx.Response(200, json={'data':{'queued':True, 'delivery_id':'a'*36}})
    monkeypatch.setattr(mail_relay.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(receive), **kwargs))
    assert mail_relay.send_access('MEMBER@example.com', {'event_id':'a'*32}) == 'a'*36
