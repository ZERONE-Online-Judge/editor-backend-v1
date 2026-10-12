import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, func
from app import shared_auth
from app.config import get_settings, Settings
from app.db import SessionLocal, User, LoginSession, LoginCode

KEY = "shared-auth-key-for-tests-at-least-32-characters"


@pytest.fixture
def central(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "zoj")
    monkeypatch.setattr(settings, "auth_api_url", "https://zoj.kr/api")
    monkeypatch.setattr(settings, "auth_bridge_secret", SecretStr(KEY))
    identity = {
        "account_id": str(uuid4()),
        "email": "author@example.com",
        "display_name": "공통 계정",
    }
    state = {"active": True, "calls": [], "identity": identity, "error": None}

    def receive(request):
        state["calls"].append(request)
        values = json.loads(request.content)
        headers = request.headers
        assert "authorization" not in headers and "cookie" not in headers
        sha = hashlib.sha256(request.content).hexdigest()
        canonical = "\n".join(
            [
                "zoj-shared-auth-v1",
                "POST",
                request.url.path,
                headers["X-Editor-Timestamp"],
                headers["X-Editor-Nonce"],
                headers["X-Editor-Identity"],
                sha,
            ]
        )
        assert (
            headers["X-Editor-Signature"]
            == hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        )
        action = request.url.path.rsplit("/", 1)[1]
        if action == "login-options":
            return httpx.Response(200, json={"data": {"password_available": True}})
        if state["error"]:
            return httpx.Response(
                401,
                json={"error": {"code": state["error"], "message": "configuration"}},
            )
        if action in {"password-login", "code-verify"}:
            state["active"] = True
            return httpx.Response(
                200,
                json={
                    "data": {
                        "user": identity,
                        "session_token": "central-refresh-token",
                        "expires_in": 123456,
                    }
                },
            )
        if action in {"password-reset-request", "code-request"}:
            return httpx.Response(200, json={"data": {"requested": True}})
        if (
            not state["active"]
            or values.get("session_token") != "central-refresh-token"
        ):
            return httpx.Response(
                401,
                json={
                    "error": {"code": "authentication_required", "message": "revoked"}
                },
            )
        if action in {"logout", "password-confirm"}:
            state["active"] = False
            return httpx.Response(200, json={"data": {"revoked": True}})
        return httpx.Response(200, json={"data": identity})

    transport = httpx.Client(transport=httpx.MockTransport(receive))
    monkeypatch.setattr(shared_auth, "client", lambda: transport)
    yield state
    transport.close()


def test_central_cookie_links_existing_profile_without_moving_projects(
    owner, problem, central
):
    owner.cookies.set(
        "zoj_sso", "central-refresh-token", domain="testserver.local", path="/"
    )
    before = owner.get("/api/auth/me").json()
    assert before["account_id"] == central["identity"]["account_id"]
    assert before["handle"] == "author"
    assert owner.get("/api/problems").json()[0]["id"] == problem
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        row = db.scalar(select(User).where(User.email == "author@example.com"))
        assert row.id == before["id"] and row.central_account_id == before["account_id"]
    central["active"] = False
    assert owner.get("/api/problems").status_code == 401
    # A stale Editor-only session cannot bypass a revoked central session.
    owner.cookies.delete("zoj_sso", domain="testserver.local", path="/")
    assert owner.get("/api/auth/me").status_code == 401


def test_password_and_email_login_return_only_profile_and_share_exact_session_lifetime(
    client, central
):
    assert client.post(
        "/api/auth/login-options", json={"email": "author@example.com"}
    ).json() == {"password_available": True}
    for route, values in [
        (
            "/api/auth/password/login",
            {"email": "author@example.com", "password": "a long test password"},
        ),
        ("/api/auth/verify", {"email": "author@example.com", "code": "123456"}),
    ]:
        result = client.post(route, json=values)
        assert result.status_code == 200, result.text
        assert result.json()["account_id"] == central["identity"]["account_id"]
        assert (
            "central-refresh-token" not in result.text and "password" not in result.text
        )
        cookie = result.headers["set-cookie"]
        assert (
            cookie.startswith("zoj_sso=")
            and "HttpOnly" in cookie
            and "Max-Age=123456" in cookie
        )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 0
        assert db.scalar(select(func.count()).select_from(LoginCode)) == 0
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert "zoj_sso" not in client.cookies


def test_bridge_misconfiguration_is_not_reported_as_user_logout(client, central):
    client.cookies.set("zoj_sso", "central-refresh-token")
    central["error"] = "editor_signature_invalid"
    result = client.get("/api/auth/me")
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "shared_auth_configuration"
    secret = "private-short"
    result = client.post(
        "/api/auth/password/confirm", json={"code": "123456", "new_password": secret}
    )
    assert result.status_code == 422 and secret not in result.text
    assert (
        client.post(
            "/api/auth/password/login",
            json={
                "email": "author@example.com",
                "password": "long enough password",
                "account_id": "spoof",
            },
        ).status_code
        == 422
    )


def test_production_cannot_use_independent_credentials_or_plaintext_transport():
    base = dict(
        env="production",
        public_origin="https://editor.zoj.kr",
        database_url="postgresql+psycopg://unused",
        session_secret="s" * 32,
        auth_bridge_secret=KEY,
    )
    for values in [
        dict(auth_mode="local"),
        dict(auth_api_url="http://zoj.kr/api"),
        dict(auth_api_url="https://user:password@zoj.kr/api"),
        dict(auth_api_url="https://zoj.kr/api?redirect=other"),
    ]:
        with pytest.raises(ValueError):
            Settings(**base, **values)
    assert (
        Settings(**base, auth_mode="zoj", auth_api_url="https://zoj.kr/api").auth_mode
        == "zoj"
    )
