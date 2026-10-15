import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app import revision_updates as updates
from app.db import LoginSession, SessionLocal, User, WorkingCopy, engine, now
from test_shared_folders import add_problem, join, shared


def commit_name(owner, problem, name='동료가 커밋한 제목'):
    state = owner.get(f'/api/problems/{problem}').json()
    state['document']['info']['name'] = name
    saved = owner.put(f'/api/problems/{problem}/working-copy', json={'version': state['version'], 'document': state['document']})
    assert saved.status_code == 200, saved.text
    result = owner.post(f'/api/problems/{problem}/commit', json={'version': saved.json()['version'], 'message': '새 제목을 공유합니다.'})
    assert result.status_code == 200, result.text
    return result.json()['revision']


def test_metadata_does_not_fetch_documents_or_create_working_copies(owner, problem, clients):
    viewer = clients('viewer')
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'viewer', 'role': 'READ'})
    queries = []
    def record(conn, cursor, statement, *args):
        queries.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        response = viewer.get(f'/api/problems/{problem}/revision-status?wait_seconds=0')
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 0
    assert set(response.json()) == {'revision', 'author', 'message', 'created_at'}
    assert response.headers['cache-control'] == 'no-store'
    assert not any('document' in sql.lower() or 'working_copies' in sql.lower() for sql in queries)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.handle == 'viewer'))
        assert db.get(WorkingCopy, (problem, user.id)) is None


def test_another_users_commit_wakes_the_waiting_request_without_loading_their_draft(owner, problem, clients, monkeypatch):
    viewer = clients('viewer')
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'viewer', 'role': 'READ'})
    ready = Event()
    original = updates.revision_status
    def status(*args):
        value = original(*args)
        ready.set()
        return value
    monkeypatch.setattr(updates, 'revision_status', status)
    monkeypatch.setattr(updates, 'POLL_SECONDS', .01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(viewer.get, f'/api/problems/{problem}/revision-status?after=0&wait_seconds=2')
        assert ready.wait(1)
        state = owner.get(f'/api/problems/{problem}').json()
        state['document']['info']['name'] = '아직 공유하지 않은 제목'
        saved = owner.put(f'/api/problems/{problem}/working-copy', json={'version': state['version'], 'document': state['document']})
        assert saved.status_code == 200
        assert not pending.done()
        assert commit_name(owner, problem) == 1
        response = pending.result(timeout=2)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 1
    assert response.json()['author'] == 'author'
    assert response.json()['message'] == '새 제목을 공유합니다.'


def test_access_rechecked_during_wait_and_session_revocation_prevents_a_reply(owner, problem, clients, monkeypatch):
    viewer = clients('viewer')
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'viewer', 'role': 'READ'})
    async def connected(): return False
    request = SimpleNamespace(cookies=viewer.cookies, is_disconnected=connected)
    with SessionLocal() as db:
        user_id = db.scalar(select(User.id).where(User.handle == 'viewer'))
    async def revoke_access(_):
        owner.put(f'/api/problems/{problem}/accesses', json={'login': 'viewer', 'role': 'NONE'})
    monkeypatch.setattr(updates.asyncio, 'sleep', revoke_access)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(updates.wait_for_revision(request, problem, user_id, 0, .1))
    assert denied.value.status_code == 404
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'viewer', 'role': 'READ'})
    async def revoke_session(_):
        with SessionLocal() as db:
            row = db.scalar(select(LoginSession).where(LoginSession.user_id == user_id))
            row.expires_at = now() - 1
            db.commit()
        commit_name(owner, problem)
    monkeypatch.setattr(updates.asyncio, 'sleep', revoke_session)
    with pytest.raises(HTTPException) as expired:
        asyncio.run(updates.wait_for_revision(request, problem, user_id, 0, .1))
    assert expired.value.status_code == 401


def test_shared_folder_viewers_can_watch_until_their_membership_is_removed(owner, clients):
    space = shared(owner)
    viewer = clients('viewer')
    join(owner, viewer, space, 'VIEWER', 'viewer')
    problem = add_problem(owner, space)
    assert viewer.get(f'/api/problems/{problem}/revision-status?wait_seconds=0').status_code == 200
    who = next(member['user_id'] for member in owner.get(f'/api/shared-folders/{space}/members').json()['members'] if member['handle'] == 'viewer')
    owner.delete(f'/api/shared-folders/{space}/members/{who}')
    assert viewer.get(f'/api/problems/{problem}/revision-status?wait_seconds=0').status_code == 404


def test_watch_rejects_unauthenticated_hidden_deleted_and_unbounded_requests(owner, problem, client, clients):
    path = f'/api/problems/{problem}/revision-status'
    assert client.get(path).status_code == 401
    assert clients('outsider').get(path).status_code == 404
    for query in ['after=-1', 'wait_seconds=-1', 'wait_seconds=26', 'wait_seconds=nan', 'wait_seconds=inf']:
        assert owner.get(path + '?' + query).status_code == 422
    assert owner.get(path + '?after=0&wait_seconds=0.01').json()['revision'] == 0
    owner.patch(f'/api/problems/{problem}/deleted', json={'deleted': True})
    assert owner.get(path).status_code == 404


def test_disconnect_releases_the_wait_without_additional_reads(monkeypatch):
    reads = []
    monkeypatch.setattr(updates, 'revision_status', lambda *args: reads.append(args) or {'revision': 0})
    async def disconnected(): return True
    response = asyncio.run(updates.wait_for_revision(SimpleNamespace(is_disconnected=disconnected), 'p', 'u', 0, 25))
    assert response.status_code == 204 and len(reads) == 1


def test_update_preserves_local_work_and_reports_same_field_conflicts(owner, problem, clients):
    writer = clients('writer')
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'writer', 'role': 'WRITE'})
    local = writer.get(f'/api/problems/{problem}').json()
    local['document']['statements']['korean']['legend'] = '내가 작성 중인 지문'
    version = writer.put(f'/api/problems/{problem}/working-copy', json={'version': local['version'], 'document': local['document']}).json()['version']
    commit_name(owner, problem)
    assert writer.get(f'/api/problems/{problem}/revision-status?after=0').json()['revision'] == 1
    assert writer.post(f'/api/problems/{problem}/update', json={'version': version}).status_code == 200
    merged = writer.get(f'/api/problems/{problem}').json()
    assert merged['document']['statements']['korean']['legend'] == '내가 작성 중인 지문'
    assert merged['document']['info']['name'] == '동료가 커밋한 제목'
    local = copy.deepcopy(merged)
    local['document']['info']['name'] = '나의 제목'
    version = writer.put(f'/api/problems/{problem}/working-copy', json={'version': local['version'], 'document': local['document']}).json()['version']
    commit_name(owner, problem, '동료의 다음 제목')
    assert writer.post(f'/api/problems/{problem}/update', json={'version': version}).status_code == 409
    assert writer.get(f'/api/problems/{problem}').json()['document']['info']['name'] == '나의 제목'
