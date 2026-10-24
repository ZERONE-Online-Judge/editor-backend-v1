"""Keep WebAuthn ceremonies on the canonical ZOJ origin and reuse its SSO cookie."""
import re
from typing import Literal
from urllib.parse import urlsplit, unquote, urlencode
from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from .config import get_settings
from .domain import fail

router = APIRouter(prefix='/api/auth', tags=['shared authentication'])


def safe_editor_path(value):
    if not value or not value.startswith('/') or value.startswith('//'):
        return '/problems'
    if '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return '/problems'
    try:
        url = urlsplit(value)
        path = unquote(url.path, errors='strict')
        if url.scheme or url.netloc or '\\' in path or not re.match(r'^/(?:problems(?:/|$)|help(?:/|$)|$)', path):
            return '/problems'
        # Normalize dot segments as the browser will before testing allowed routes.
        from posixpath import normpath
        normalized = normpath(path)
        if not re.match(r'^/(?:problems(?:/|$)|help(?:/|$)|$)', normalized):
            return '/problems'
        return value
    except (ValueError, UnicodeError):
        return '/problems'


def require_central_login():
    if get_settings().auth_mode == 'zoj':
        fail('central_login_required', 'ZOJ 공통 로그인에서 패스키·이메일·비밀번호로 인증해 주세요.', 409, login_url='/api/auth/redirect')


@router.get('/config')
def config():
    return {'mode': get_settings().auth_mode, 'login_url': '/api/auth/redirect'}


@router.get('/redirect')
def login_redirect(next: str = '/problems'):
    if get_settings().auth_mode != 'zoj':
        return RedirectResponse('/login', status_code=302)
    destination = '/editor/return?' + urlencode({'next': safe_editor_path(next)})
    return RedirectResponse(get_settings().zoj_public_origin.rstrip('/') + '/login?' + urlencode({'moveTo': destination}), status_code=302)


@router.get('/security')
def security_redirect(section: Literal['passkeys', 'password'] = 'passkeys', next: str = '/problems'):
    if get_settings().auth_mode != 'zoj':
        fail('shared_auth_not_configured', 'ZOJ 공통 인증이 필요합니다.', 503)
    return RedirectResponse(get_settings().zoj_public_origin.rstrip('/') + '/editor/account?' + urlencode({'section': section, 'next': safe_editor_path(next)}), status_code=302)
