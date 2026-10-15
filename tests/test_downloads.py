import io
import zipfile

import pytest

from app.config import get_settings


def upload(client, problem, content):
    response = client.post(f'/api/problems/{problem}/blobs', content=content)
    assert response.status_code == 201
    return response.json()['sha256']


def payload(sha, path='tests/tests/0001.in'):
    return {'filename': 'selected-tests.zip', 'files': [{'path': path, 'sha256': sha}]}


def test_selected_zip_preserves_binary_empty_files_paths_and_working_copy(owner, problem):
    before = owner.get(f'/api/problems/{problem}').json()
    contents = [b'2\r\n\x00\xff\n', b'', b'print("hello")\n']
    hashes = [upload(owner, problem, content) for content in contents]
    paths = ['tests/tests/0001.in', 'tests/tests/0001.out', 'solutions/main.py']
    request = {'filename': 'selected.zip', 'files': [
        {'path': name, 'sha256': sha} for name, sha in zip(paths, hashes)
    ]}
    response = owner.post(f'/api/problems/{problem}/downloads', json=request)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['content-disposition'] == 'attachment; filename="selected.zip"'
    assert int(response.headers['content-length']) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == paths
        assert [archive.read(name) for name in paths] == contents
        assert archive.testzip() is None
    after = owner.get(f'/api/problems/{problem}').json()
    assert (before['document'], before['version']) == (after['document'], after['version'])


def test_download_allows_readers_but_checks_revocation_and_problem_blob_ownership(owner, problem, clients):
    reader = clients('reader')
    outsider = clients('outsider')
    sha = upload(owner, problem, b'current-or-stored-execution-output')
    url = f'/api/problems/{problem}/downloads'
    assert outsider.post(url, json=payload(sha)).status_code == 404
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'reader', 'role': 'READ'})
    assert reader.post(url, json=payload(sha)).status_code == 200
    other = owner.post('/api/problems', json={'name': 'Other', 'slug': 'other-task'}).json()['id']
    assert owner.post(f'/api/problems/{other}/downloads', json=payload(sha)).status_code == 404
    assert owner.post(url, json=payload('0' * 64)).status_code == 404
    owner.put(f'/api/problems/{problem}/accesses', json={'login': 'reader', 'role': 'NONE'})
    assert reader.post(url, json=payload(sha)).status_code == 404


def test_download_honors_inherited_shared_folder_access(owner, clients):
    viewer = clients('viewer')
    space = owner.post('/api/shared-folders', json={'name': '공유'}).json()['id']
    problem = owner.post('/api/problems', json={'name': 'Shared', 'slug': 'shared-task', 'folder_id': space}).json()['id']
    invitation = owner.post(f'/api/shared-folders/{space}/invitations', json={'email': 'viewer@example.com', 'role': 'VIEWER'}).json()['id']
    assert viewer.post(f'/api/shared-folders/invitations/{invitation}/accept').status_code == 200
    sha = upload(owner, problem, b'shared-data')
    assert viewer.post(f'/api/problems/{problem}/downloads', json=payload(sha)).status_code == 200
    member = next(row for row in owner.get(f'/api/shared-folders/{space}/members').json()['members'] if row['handle'] == 'viewer')
    owner.delete(f'/api/shared-folders/{space}/members/{member["user_id"]}')
    assert viewer.post(f'/api/problems/{problem}/downloads', json=payload(sha)).status_code == 404


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a/../../secret', 'a//b', 'a\\b', 'C:/file', './file', 'a/./b', 'a/b/c/d/e', 'a/\x00b', 'a/line\nbreak'])
def test_archive_rejects_unsafe_paths(owner, problem, path):
    assert owner.post(f'/api/problems/{problem}/downloads', json=payload('a' * 64, path)).status_code == 422


def test_archive_rejects_duplicate_and_conflicting_paths_and_invalid_selection(owner, problem):
    sha = upload(owner, problem, b'data')
    url = f'/api/problems/{problem}/downloads'
    for paths in [['a.in', 'a.in'], ['A.in', 'a.in'], ['files', 'files/main.cpp']]:
        response = owner.post(url, json={'files': [{'path': path, 'sha256': sha} for path in paths]})
        assert response.status_code == 400
    assert owner.post(url, json={'files': []}).status_code == 422
    assert owner.post(url, json={**payload(sha), 'filename': '../secret.zip'}).status_code == 422
    assert owner.post(url, json={**payload(sha), 'filename': 'file.html'}).status_code == 422


def test_download_bounds_repeated_content_and_requires_auth_and_csrf(owner, problem, client, monkeypatch):
    sha = upload(owner, problem, b'data')
    url = f'/api/problems/{problem}/downloads'
    assert client.post(url, json=payload(sha)).status_code == 401
    assert owner.post(url, json=payload(sha), headers={'X-Editor-Request': '0'}).status_code == 403
    assert owner.post(url, json=payload(sha), headers={'Origin': 'https://elsewhere.test'}).status_code == 403
    monkeypatch.setattr(get_settings(), 'max_project_bytes', 6)
    response = owner.post(url, json={'files': [{'path': name, 'sha256': sha} for name in ['1.in', '2.in']]})
    assert response.status_code == 413
