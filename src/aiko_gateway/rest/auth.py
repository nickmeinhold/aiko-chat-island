"""Auth endpoints: register (gated), login, refresh, me.

/register is open in dev for testing and closed by default in production
(settings.open_registration, resolved by environment). With open registration a
self-created account can read everything until I2 membership lands (#36), so
prod fails closed; an explicit OPEN_REGISTRATION override re-opens it.
"""
from __future__ import annotations

import jwt
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..domain import security, users_service
from ..domain.models import User
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class RegisterReq(BaseModel):
    username: str
    display_name: str = ""
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class RefreshReq(BaseModel):
    refresh_token: str


def _user_view(u: User) -> dict:
    return {"user_id": u.id, "username": u.username,
            "display_name": u.display_name, "aiko_username": u.aiko_username}


def _tokens(user_id: str) -> dict:
    return {"access_token": security.issue_access(user_id),
            "refresh_token": security.issue_refresh(user_id)}


@router.post("/register")
async def register(req: RegisterReq, session: DbSession) -> dict:
    if not settings.open_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed")
    try:
        user = await users_service.create_user(
            session, username=req.username,
            display_name=req.display_name, password=req.password,
        )
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    return {**_tokens(user.id), "user": _user_view(user)}


@router.post("/login")
async def login(req: LoginReq, session: DbSession) -> dict:
    user = await users_service.authenticate(session, req.username, req.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {**_tokens(user.id), "user": _user_view(user)}


@router.post("/refresh")
async def refresh(req: RefreshReq) -> dict:
    try:
        user_id = security.decode_token(req.refresh_token, expected_type="refresh")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    return {"access_token": security.issue_access(user_id)}


me_router = APIRouter(prefix="/v1", tags=["auth"])


@me_router.get("/me")
async def me(user: CurrentUser) -> dict:
    return _user_view(user)
