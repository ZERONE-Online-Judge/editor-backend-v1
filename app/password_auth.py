"""Password operations always use the central ZOJ credential store."""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import EmailStr, Field
from sqlalchemy.orm import Session
from .db import session_dependency
from .schema import Strict
from . import shared_auth

router = APIRouter(prefix="/api/auth/password", tags=["shared passwords"])


class Email(Strict):
    email: EmailStr


class Login(Email):
    password: str = Field(min_length=1, max_length=128)
    force_new_session: bool = False


class Confirm(Strict):
    code: str = Field(pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=15, max_length=128)


class Reset(Confirm):
    email: EmailStr


@router.get("")
def status(request: Request):
    return shared_auth.call(request, "password-status")


@router.post("/request")
def request_password(request: Request):
    return shared_auth.call(request, "password-request")


@router.post("/confirm")
def confirm(values: Confirm, request: Request, response: Response):
    result = shared_auth.call(request, "password-confirm", values.model_dump())
    shared_auth.clear_cookie(response)
    return result


@router.post("/reset/request")
def request_reset(values: Email, request: Request):
    return shared_auth.call(
        request, "password-reset-request", values.model_dump(mode="json")
    )


@router.post("/reset/confirm")
def reset(values: Reset, request: Request, response: Response):
    result = shared_auth.call(
        request, "password-reset-confirm", values.model_dump(mode="json")
    )
    shared_auth.clear_cookie(response)
    return result


@router.post("/login")
def login(
    values: Login,
    request: Request,
    response: Response,
    db: Session = Depends(session_dependency),
):
    return shared_auth.finish(
        request,
        response,
        db,
        shared_auth.call(request, "password-login", values.model_dump(mode="json")),
    )
