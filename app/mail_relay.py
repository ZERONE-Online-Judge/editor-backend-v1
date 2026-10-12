"""Fixed-purpose email relay; no account tokens or arbitrary mail content."""
import base64
import hashlib
import hmac
import json
import ssl
from uuid import uuid4
import httpx
from .config import get_settings
from .db import now


def send(email, code, purpose):
    settings = get_settings()
    path = '/api/editor/mail/verification'
    body = json.dumps({'code':code,'purpose':purpose}, separators=(',',':')).encode()
    encoded = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip('=')
    stamp, nonce = str(now()), uuid4().hex
    sha = hashlib.sha256(body).hexdigest()
    message = '\n'.join(['zoj-editor-mail-v1','POST',path,stamp,nonce,encoded,sha])
    headers = {'X-Editor-Identity':encoded,'X-Editor-Timestamp':stamp,'X-Editor-Nonce':nonce,
        'X-Editor-Content-Sha256':sha,'X-Editor-Signature':hmac.new(settings.mail_relay_secret.get_secret_value().encode(),message.encode(),hashlib.sha256).hexdigest(),
        'Content-Type':'application/json'}
    verify = ssl.create_default_context(cafile=settings.mail_relay_ca_file) if settings.mail_relay_ca_file else True
    with httpx.Client(timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False,verify=verify) as client:
        with client.stream('POST',settings.mail_relay_url.rstrip('/')+path.removeprefix('/api'),headers=headers,content=body) as response:
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result)>32768:
                    raise RuntimeError('Mail relay response too large')
            if response.status_code != 200 or json.loads(result).get('data',{}).get('queued') is not True:
                raise RuntimeError('ZOJ mail relay did not accept the authentication email')
