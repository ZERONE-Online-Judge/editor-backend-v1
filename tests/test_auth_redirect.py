from urllib.parse import urlsplit, parse_qs
import pytest
from app.config import get_settings
from app.auth_redirect import safe_editor_path
from test_shared_auth import central


@pytest.mark.parametrize('path', ['/problems', '/problems/abc/statements?language=ko#source', '/help', '/'])
def test_safe_paths_are_preserved(path):
    assert safe_editor_path(path) == path


@pytest.mark.parametrize('path', ['https://evil.test', '//evil.test', '/\\evil.test', '/login', '/logout', '/api/auth/redirect', '/problems/../../login', '/%2f%2fevil.test', '/problems/../../../%2f%2fevil.test', '/problems/\nsource'])
def test_redirects_reject_external_paths_and_login_loops(path):
    assert safe_editor_path(path) == '/problems'


def test_login_and_security_redirects_never_carry_credentials(client, central):
    path = '/problems/abc/files?file=main.cpp#source'
    config = client.get('/api/auth/config').json()
    assert config == {'mode': 'zoj', 'login_url': '/api/auth/redirect'}
    login = client.get('/api/auth/redirect', params={'next': path}, follow_redirects=False)
    assert login.status_code == 302
    target = urlsplit(login.headers['location'])
    assert target.scheme == 'https' and target.netloc == 'zoj.kr' and target.path == '/login'
    continuation = urlsplit(parse_qs(target.query)['moveTo'][0])
    assert continuation.path == '/editor/return'
    assert parse_qs(continuation.query)['next'] == [path]
    for section in ['passkeys', 'password']:
        result = client.get('/api/auth/security', params={'section': section, 'next': path}, follow_redirects=False)
        target = urlsplit(result.headers['location'])
        assert target.netloc == 'zoj.kr' and target.path == '/editor/account'
        assert parse_qs(target.query) == {'section': [section], 'next': [path]}
        assert 'token' not in result.headers['location'] and 'author@' not in result.headers['location']
    assert client.get('/api/auth/security?section=invalid').status_code == 422
    assert not central['calls']


def test_local_mode_retains_independent_development_login(client):
    assert client.get('/api/auth/config').json()['mode'] == 'local'
    assert client.get('/api/auth/redirect', follow_redirects=False).headers['location'] == '/login'
