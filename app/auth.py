import hashlib
import hmac
import json
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from fastapi import APIRouter, Depends, Request, Response
from pydantic import EmailStr, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import get_settings
from .db import LoginCode, LoginSession, RateLimit, User, now, session_dependency, uid
from .domain import fail
from .schema import Strict

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "editor_session"


def digest(value):
    return hmac.new(get_settings().session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def check_browser_request(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("x-editor-request") != "1":
        fail("csrf", "요청을 다시 확인해 주세요.", 403)
    origin = request.headers.get("origin")
    allowed = {get_settings().public_origin.rstrip("/")}
    if get_settings().env != "production":
        allowed.update({"http://localhost:5190", "http://127.0.0.1:5190"})
    if origin and origin.rstrip("/") not in allowed:
        fail("origin", "허용되지 않은 요청 경로입니다.", 403)


def current_user(request: Request, db: Session = Depends(session_dependency)):
    token = request.cookies.get(COOKIE, "")
    session = db.get(LoginSession, digest(token)) if token else None
    if not session or session.expires_at <= now():
        fail("authentication_required", "로그인이 필요합니다.", 401)
    user = db.get(User, session.user_id)
    if not user:
        fail("authentication_required", "로그인이 필요합니다.", 401)
    return user


def user_json(user):
    return {"id": user.id, "email": user.email, "handle": user.handle, "display_name": user.display_name}


def rate_limit(db, key, limit, seconds):
    at = now()
    row = db.get(RateLimit, key)
    if not row:
        db.add(RateLimit(key=key, started_at=at, count=1))
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            row = db.get(RateLimit, key)
    if at - row.started_at >= seconds:
        db.execute(update(RateLimit).where(RateLimit.key == key, RateLimit.started_at == row.started_at).values(started_at=at, count=1))
        db.commit()
        return
    result = db.execute(update(RateLimit).where(RateLimit.key == key, RateLimit.count < limit).values(count=RateLimit.count + 1))
    db.commit()
    if not result.rowcount:
        fail("rate_limited", "잠시 후 다시 시도해 주세요.", 429)


def send_code(email, code):
    settings = get_settings()
    if settings.mail_mode == "console":
        directory = settings.data_dir / "development-mail"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / f"{uid()}.json"
        with target.open("w") as out:
            json.dump({"email": email, "code": code, "created_at": now()}, out)
        target.chmod(0o600)
        return
    mail = EmailMessage()
    mail["Subject"] = "[ZOJ Editor] 로그인 인증번호"
    mail["From"] = settings.mail_from
    mail["To"] = email
    mail.set_content(f"ZOJ Editor 인증번호: {code}\n\n5분 동안 사용할 수 있습니다. 요청하지 않았다면 이 메일을 무시하세요.")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(mail)


class RequestCode(Strict):
    email: EmailStr


class VerifyCode(RequestCode):
    code: str = Field(pattern=r"^\d{6}$")
    display_name: str = Field(default="", max_length=80)


@router.post("/code")
def request_code(values: RequestCode, request: Request, db: Session = Depends(session_dependency)):
    check_browser_request(request)
    email = str(values.email).lower()
    rate_limit(db, "request:" + digest(request.client.host), 30, 3600)
    rate_limit(db, "email:" + digest(email), 10, 3600)
    row = db.scalar(select(LoginCode).where(LoginCode.email == email).with_for_update())
    at = now()
    if row and at - row.requested_at < 30:
        fail("cooldown", f"{30 - (at - row.requested_at)}초 후 다시 요청해 주세요.", 429)
    code = f"{secrets.randbelow(1000000):06d}"
    if not row:
        row = LoginCode(email=email, digest="", requested_at=at, expires_at=at, attempts=0, window_started=at, request_count=1)
        db.add(row)
    row.digest = digest(email + ":" + code)
    row.requested_at, row.expires_at, row.attempts = at, at + 300, 0
    db.commit()
    try:
        send_code(email, code)
    except Exception:
        fail("mail_unavailable", "인증 메일을 보내지 못했습니다. 잠시 후 다시 시도해 주세요.", 503)
    return {"sent": True, "cooldown_seconds": 30, "valid_seconds": 300}


@router.post("/verify")
def verify_code(values: VerifyCode, request: Request, response: Response, db: Session = Depends(session_dependency)):
    check_browser_request(request)
    email = str(values.email).lower()
    rate_limit(db, "verify:" + digest(request.client.host), 60, 3600)
    row = db.scalar(select(LoginCode).where(LoginCode.email == email).with_for_update())
    if not row or row.expires_at <= now() or row.attempts >= 5:
        fail("invalid_code", "인증번호가 만료되었거나 요청 횟수를 초과했습니다.", 401)
    candidate = digest(email + ":" + values.code)
    if not hmac.compare_digest(row.digest, candidate):
        db.execute(update(LoginCode).where(LoginCode.email == email, LoginCode.digest == row.digest).values(attempts=LoginCode.attempts + 1))
        db.commit()
        fail("invalid_code", "인증번호가 올바르지 않습니다.", 401)
    consumed = db.execute(delete(LoginCode).where(LoginCode.email == email, LoginCode.digest == candidate, LoginCode.expires_at > now(), LoginCode.attempts < 5))
    if not consumed.rowcount:
        db.rollback()
        fail("invalid_code", "이미 사용한 인증번호입니다.", 401)
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, handle="editor-" + uid()[:10], display_name=values.display_name.strip() or email.split("@")[0])
        db.add(user)
        db.flush()
    token = secrets.token_urlsafe(32)
    settings = get_settings()
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(delete(LoginSession).where(LoginSession.digest == digest(old)))
    db.add(LoginSession(digest=digest(token), user_id=user.id, expires_at=now() + settings.session_days * 86400))
    db.commit()
    response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True, secure=settings.env == "production", samesite="lax", path="/")
    return user_json(user)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_json(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(session_dependency)):
    check_browser_request(request)
    if request.cookies.get(COOKIE):
        db.execute(delete(LoginSession).where(LoginSession.digest == digest(request.cookies[COOKIE])))
        db.commit()
    response.delete_cookie(COOKIE, path="/", secure=get_settings().env == "production", httponly=True, samesite="lax")
    return {"ok": True}


class Profile(Strict):
    display_name: str = Field(min_length=1, max_length=80)
    handle: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{2,39}$")


@router.patch("/profile")
def update_profile(values: Profile, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    if db.scalar(select(User).where(User.handle == values.handle, User.id != user.id)):
        fail("handle_taken", "이미 사용 중인 아이디입니다.")
    row = db.get(User, user.id)
    row.display_name, row.handle = values.display_name.strip(), values.handle
    db.commit()
    return user_json(row)
