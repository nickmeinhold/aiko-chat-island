"""Auth endpoints: register (gated), login, refresh, me.

/register is open in dev for testing and closed by default in production
(settings.open_registration, resolved by environment). With open registration a
self-created account can read everything until I2 membership lands (#36), so
prod fails closed; an explicit OPEN_REGISTRATION override re-opens it.
"""
from __future__ import annotations

import jwt
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..domain import accounts_service, oauth, security, users_service
from ..domain.models import User
from ..domain.oauth import Provider
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
        # Roll back the failed transaction before reusing/closing the session
        # (a failed commit leaves it needing rollback).
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    return {**_tokens(user.id), "user": _user_view(user)}


@router.post("/login")
async def login(req: LoginReq, session: DbSession) -> dict:
    user = await users_service.authenticate(session, req.username, req.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {**_tokens(user.id), "user": _user_view(user)}


class SocialReq(BaseModel):
    provider: Provider     # closed set → a bad value is a 422 at the boundary
    id_token: str          # the provider ID token obtained on-device


class SocialClaimReq(BaseModel):
    provisioning_token: str
    # Bounded at the public front door (cage-match PR#15): a handle becomes the
    # username + aiko_username (both String(64)); reject empty/whitespace/overlong
    # here with a 422 rather than deferring to DB behaviour and 409/500 ambiguity.
    handle: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)

    @field_validator("handle", "display_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("handle")
    @classmethod
    def _handle_nonempty_after_strip(cls, v: str) -> str:
        if not v:
            raise ValueError("handle must not be blank")
        return v


@router.post("/social")
async def social(req: SocialReq, session: DbSession) -> dict:
    """Verify a provider ID token. Known identity → real tokens. Brand-new
    identity → a short-lived provisioning token to carry the verified identity
    into /social/claim (no DB row until the user picks a handle)."""
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    try:
        identity = await oauth.verify_id_token(req.provider, req.id_token)
    except oauth.UnknownProvider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown provider")
    except oauth.ProviderUnavailable:
        # Provider/JWKS outage — transient, not a bad credential. 503 so clients
        # back off rather than treating it as an auth failure (401) and retrying.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "provider temporarily unavailable")
    except oauth.InvalidProviderToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid provider token")

    user = await users_service.get_user_by_social(
        session, identity.provider, identity.sub)
    if user is not None:
        return {**_tokens(user.id), "user": _user_view(user)}

    # New federated identity → hand back a signed provisioning token (the pending
    # state). The client then POSTs /social/claim with a chosen handle.
    provisioning_token = security.issue_provisioning(
        identity.provider, identity.sub,
        suggested_name=identity.suggested_name, email=identity.email,
    )
    return {
        "status": "provisioning",
        "provisioning_token": provisioning_token,
        "suggested_name": identity.suggested_name,
        "email": identity.email,
    }


@router.post("/social/claim")
async def social_claim(req: SocialClaimReq, session: DbSession) -> dict:
    """Complete provisioning: verify the provisioning token (OUR token, so the
    (provider, sub) it carries cannot be forged), create the user + social link
    atomically, and return real tokens."""
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    try:
        pending = security.decode_provisioning(req.provisioning_token)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired provisioning token")
    try:
        user = await users_service.create_social_user(
            session,
            provider=pending["provider"], provider_sub=pending["provider_sub"],
            handle=req.handle, display_name=req.display_name,
            email=pending["email"],
        )
    except IntegrityError:
        # Handle already taken OR this identity was already claimed (race).
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "handle already taken or identity already claimed")
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


@me_router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(user: CurrentUser, session: DbSession) -> Response:
    """Permanently delete the authenticated user's account (Apple 5.1.1(v)).

    Hard-deletes the user row + federated identities + channel memberships and
    anonymizes the user's authored messages (the conversation survives, the
    account link does not). 409 if the user is the sole admin of any channel —
    they must hand those over or leave them first."""
    try:
        await accounts_service.delete_user_account(session, user.id)
    except accounts_service.CannotDeleteSoleAdmin as e:
        # The guard runs before any write, so there is nothing to roll back.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cannot delete account while sole admin of channel(s) "
            f"{', '.join(e.channel_ids)}; transfer or leave them first",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
