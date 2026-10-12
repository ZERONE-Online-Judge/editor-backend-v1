import hashlib
import hmac
import httpx
import pytest
from pydantic import SecretStr
from app.config import get_settings, Settings
from app import mail_relay
from app.auth import send_code


def test_mail_uses_separate_signed_fixed_origin_without_user_tokens(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings,'mail_mode','zoj')
    monkeypatch.setattr(settings,'mail_relay_url','https://zoj.kr/api')
    monkeypatch.setattr(settings,'mail_relay_secret',SecretStr('relay-key-at-least-32-characters-long'))
    real_client = httpx.Client
    calls = []
    def receive(request):
        calls.append(request)
        headers = request.headers
        assert 'authorization' not in headers and 'cookie' not in headers
        sha = hashlib.sha256(request.content).hexdigest()
        canonical = '\n'.join(['zoj-editor-mail-v1','POST',request.url.path,headers['X-Editor-Timestamp'],headers['X-Editor-Nonce'],headers['X-Editor-Identity'],sha])
        assert headers['X-Editor-Signature'] == hmac.new(settings.mail_relay_secret.get_secret_value().encode(),canonical.encode(),hashlib.sha256).hexdigest()
        return httpx.Response(200,json={'data':{'queued':True}})
    monkeypatch.setattr(mail_relay.httpx,'Client',lambda **kwargs: real_client(transport=httpx.MockTransport(receive),**kwargs))
    send_code('author@example.com','123456','password_set')
    assert str(calls[0].url) == 'https://zoj.kr/api/editor/mail/verification'
    assert calls[0].content == b'{"code":"123456","purpose":"password_set"}'


def test_relay_configuration_rejects_unfixed_targets_and_production_http():
    for url in ['https://user:pass@zoj.kr/api','https://zoj.kr/api?target=elsewhere','https://zoj.kr/other']:
        with pytest.raises(ValueError):
            Settings(mail_mode='zoj',mail_relay_url=url,mail_relay_secret='x'*32)
    with pytest.raises(ValueError):
        Settings(env='production',mail_mode='zoj',mail_relay_url='http://zoj.kr/api',mail_relay_secret='x'*32)
