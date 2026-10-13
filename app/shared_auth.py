"""ZOJ owns identity, password and session; Editor owns projects."""

import base64
import hashlib
import hmac
import ipaddress
import json
import ssl
import time
from functools import lru_cache
from uuid import uuid4
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .config import get_settings
from .db import User
from .domain import fail

COOKIE = "zoj_sso"


def domain():
    s = get_settings()
    return s.sso_cookie_domain or (".zoj.kr" if s.env == "production" else None)


def clear_cookie(response):
    response.delete_cookie(
        COOKIE,
        path="/",
        domain=domain(),
        httponly=True,
        secure=get_settings().env == "production",
        samesite="lax",
    )


def set_cookie(response, token, max_age):
    response.set_cookie(
        COOKIE,
        token,
        max_age=max_age,
        path="/",
        domain=domain(),
        httponly=True,
        secure=get_settings().env == "production",
        samesite="lax",
    )


def signed(action, values):
    s = get_settings()
    if s.auth_mode != "zoj" or not s.auth_api_url or not s.auth_bridge_secret:
        fail(
            "shared_auth_not_configured",
            "ZOJ 로그인이 아직 설정되지 않았습니다.",
            503,
        )
    path = "/api/editor/auth/" + action
    body = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(b"editor@zoj.kr").decode().rstrip("=")
    stamp, nonce, sha = (
        str(int(time.time())),
        uuid4().hex,
        hashlib.sha256(body).hexdigest(),
    )
    message = "\n".join(
        ["zoj-shared-auth-v1", "POST", path, stamp, nonce, encoded, sha]
    )
    headers = {
        "Content-Type": "application/json",
        "X-Editor-Identity": encoded,
        "X-Editor-Timestamp": stamp,
        "X-Editor-Nonce": nonce,
        "X-Editor-Content-Sha256": sha,
        "X-Editor-Signature": hmac.new(
            s.auth_bridge_secret.get_secret_value().encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest(),
    }
    return s.auth_api_url.rstrip("/") + path.removeprefix("/api"), body, headers


def values_for(request, values=None):
    result = dict(values or {})
    if request.cookies.get(COOKIE):
        result["session_token"] = request.cookies[COOKIE]
    try:
        result["client_ip"] = str(ipaddress.ip_address(request.client.host))
    except ValueError:
        result["client_ip"] = "127.0.0.1"
    return result


@lru_cache
def client():
    s = get_settings()
    verify = (
        ssl.create_default_context(cafile=s.auth_ca_file) if s.auth_ca_file else True
    )
    return httpx.Client(
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=False,
        trust_env=False,
        verify=verify,
    )


def check_response(response, body):
    if response.is_redirect:
        fail("shared_auth_redirect", "ZOJ 로그인 서버 주소를 확인하세요.", 502)
    try:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError()
    except ValueError:
        fail("shared_auth_invalid", "ZOJ 로그인 서버 응답이 올바르지 않습니다.", 502)
    if response.is_error:
        error = result.get("error", {})
        if not isinstance(error, dict):
            fail("shared_auth_invalid", "ZOJ 로그인 서버 응답이 올바르지 않습니다.", 502)
        if error.get("code") in {
            "editor_signature_invalid",
            "editor_signature_replayed",
            "editor_bridge_disabled",
        }:
            fail(
                "shared_auth_configuration",
                "ZOJ 로그인 연결 설정을 관리자에게 확인해 주세요.",
                503,
            )
        fail(
            error.get("code", "shared_auth_failed"),
            error.get("message", "ZOJ 로그인 요청에 실패했습니다."),
            response.status_code,
            **(error.get("details") or {}),
        )
    if not isinstance(result.get("data"), dict):
        fail("shared_auth_invalid", "ZOJ 로그인 서버 응답이 올바르지 않습니다.", 502)
    return result["data"]


def call(request, action, values=None):
    target, body, headers = signed(action, values_for(request, values))
    try:
        with client().stream("POST", target, content=body, headers=headers) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 65536:
                    fail("shared_auth_invalid", "ZOJ 로그인 응답이 너무 큽니다.", 502)
            return check_response(response, raw)
    except (httpx.HTTPError, OSError):
        fail(
            "shared_auth_unavailable",
            "ZOJ 로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.",
            503,
        )


def sync_user(db, identity):
    email, account_id = identity["email"].lower(), identity["account_id"]
    user = db.scalar(select(User).where(User.central_account_id == account_id))
    if not user:
        user = db.scalar(select(User).where(User.email == email))
        if user and user.central_account_id not in {None, account_id}:
            fail(
                "account_link_conflict",
                "계정 연결 상태를 관리자에게 확인해 주세요.",
                409,
            )
    changed = False
    if not user:
        user = User(
            email=email,
            central_account_id=account_id,
            handle="editor-" + uuid4().hex[:10],
            display_name=identity["display_name"][:80],
        )
        db.add(user)
        changed = True
    if (
        user.central_account_id != account_id
        or user.email != email
        or user.display_name != identity["display_name"][:80]
    ):
        user.central_account_id, user.email, user.display_name = (
            account_id,
            email,
            identity["display_name"][:80],
        )
        changed = True
    if changed:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Concurrent first visits may both link the same legacy profile.
            user = db.scalar(
                select(User).where(
                    User.central_account_id == account_id, User.email == email
                )
            )
            if not user:
                fail(
                    "account_link_conflict",
                    "계정 연결 상태를 관리자에게 확인해 주세요.",
                    409,
                )
    return user


def current_user(request, db):
    if not request.cookies.get(COOKIE):
        fail("authentication_required", "로그인이 필요합니다.", 401)
    return sync_user(db, call(request, "me"))


def finish(request, response, db, result):
    user = sync_user(db, result["user"])
    set_cookie(response, result["session_token"], result["expires_in"])
    from .auth import user_json

    return user_json(user)


async def session_events(request):
    target, body, headers = signed("events", values_for(request))
    s = get_settings()
    verify = (
        ssl.create_default_context(cafile=s.auth_ca_file) if s.auth_ca_file else True
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=25, connect=5),
        verify=verify,
        follow_redirects=False,
        trust_env=False,
    ) as remote:
        async with remote.stream(
            "POST", target, content=body, headers=headers
        ) as response:
            if response.status_code == 401:
                yield "event: session_revoked\ndata: {}\n\n"
                return
            if response.status_code != 200:
                return
            async for chunk in response.aiter_bytes():
                if await request.is_disconnected():
                    return
                yield chunk
