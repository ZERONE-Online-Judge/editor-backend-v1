"""Fixed-purpose email relay; no account tokens or arbitrary mail content."""
import base64
import hashlib
import hmac
import json
import logging
import ssl
from uuid import uuid4
import httpx
from .config import get_settings
from .db import now


def send(email, code, purpose):
    settings = get_settings()
    return post(email, '/api/editor/mail/verification', {'code':code,'purpose':purpose},
        url=settings.mail_relay_url, key=settings.mail_relay_secret,
        ca_file=settings.mail_relay_ca_file, purpose='zoj-editor-mail-v1')


def send_access(email, payload):
    settings = get_settings()
    if settings.auth_mode != 'zoj':
        logging.getLogger('editor.access_mail').info('Local access notification %s', payload['event_id'])
        return 'console'
    # Shared login already provisions this private TLS route and key. A separate
    # HMAC purpose prevents notification signatures being used as login calls.
    result = post(email, '/api/editor/mail/access', payload, url=settings.auth_api_url,
        key=settings.auth_bridge_secret, ca_file=settings.auth_ca_file,
        purpose='zoj-editor-access-mail-v1')
    delivery_id = result.get('delivery_id')
    if not isinstance(delivery_id, str) or len(delivery_id) != 36:
        raise RuntimeError('Mail relay did not return a delivery receipt')
    return delivery_id


def post(email, path, values, *, url, key, ca_file, purpose):
    body = json.dumps(values, ensure_ascii=False, separators=(',',':')).encode()
    encoded = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip('=')
    stamp, nonce = str(now()), uuid4().hex
    sha = hashlib.sha256(body).hexdigest()
    message = '\n'.join([purpose,'POST',path,stamp,nonce,encoded,sha])
    headers = {'X-Editor-Identity':encoded,'X-Editor-Timestamp':stamp,'X-Editor-Nonce':nonce,
        'X-Editor-Content-Sha256':sha,'X-Editor-Signature':hmac.new(key.get_secret_value().encode(),message.encode(),hashlib.sha256).hexdigest(),
        'Content-Type':'application/json'}
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with httpx.Client(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False,verify=verify) as client:
        with client.stream('POST',url.rstrip('/')+path.removeprefix('/api'),headers=headers,content=body) as response:
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result)>32768:
                    raise RuntimeError('Mail relay response too large')
            data = json.loads(result).get('data',{}) if response.status_code == 200 else {}
            if data.get('queued') is not True:
                raise RuntimeError('ZOJ mail relay did not accept the email')
            return data
