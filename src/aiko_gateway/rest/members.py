"""Membership-management endpoints (#46) — the WRITE side of the I2 boundary.

Before this, private channels were inert: #36 enforces that a private channel
needs a Membership row to read/post, but nothing could CREATE one. These routes
add channel creation (creator auto-admin), admin add/remove, and policy-gated
self-join / leave.

I1 (auth): every route takes ``CurrentUser`` so an unauthenticated caller is
rejected before any row is touched. The trust-boundary rules themselves live in
``memberships_service`` (single enforcement source, mirroring ``acl``); this
layer only translates the service's typed rejections into HTTP — crucially
mapping the existence-hiding errors (ChannelNotFound, and NotAMember on
leave) to 404 so a non-member can never distinguish "private channel I can't
see" from "no such channel" (existence-hiding, #36).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..domain import memberships_service as svc
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["members"])


class CreateChannelReq(BaseModel):
    name: str
    is_private: bool = False
    # Self-join policy for a private channel: 'invite_only' (default) | 'open'.
    join_policy: str = svc.JOIN_INVITE_ONLY


class AddMemberReq(BaseModel):
    user_id: str
    role: str = svc.ROLE_MEMBER
    can_post: bool = True


def _channel_view(c) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "aiko_channel": c.aiko_channel,
        "is_private": c.is_private,
        "join_policy": c.join_policy,
    }


def _member_view(m) -> dict:
    return {"user_id": m.user_id, "role": m.role, "can_post": m.can_post}


@router.post("/channels", status_code=201)
async def create_channel(req: CreateChannelReq, user: CurrentUser, session: DbSession) -> dict:
    channel = await svc.create_channel(
        session,
        creator_id=user.id,
        name=req.name,
        is_private=req.is_private,
        join_policy=req.join_policy,
    )
    return _channel_view(channel)


@router.get("/channels/{channel_id}/members")
async def list_members(channel_id: str, user: CurrentUser, session: DbSession) -> dict:
    try:
        members = await svc.list_members(session, channel_id=channel_id, actor_id=user.id)
    except svc.ChannelNotFound:
        # Existence-hiding: a non-member of a private channel gets the SAME 404
        # as a nonexistent channel — never a confirmation it exists.
        raise HTTPException(404, "channel not found")
    return {"channel_id": channel_id, "members": [_member_view(m) for m in members]}


@router.post("/channels/{channel_id}/members", status_code=201)
async def add_member(
    channel_id: str, req: AddMemberReq, user: CurrentUser, session: DbSession
) -> dict:
    try:
        m = await svc.add_member(
            session,
            channel_id=channel_id,
            actor_id=user.id,
            target_user_id=req.user_id,
            role=req.role,
            can_post=req.can_post,
        )
    except svc.ChannelNotFound:
        raise HTTPException(404, "channel not found")
    except svc.NotChannelAdmin:
        # A member who is not an admin already knows the channel exists, so an
        # honest 403 leaks nothing.
        raise HTTPException(403, "only a channel admin may add members")
    return _member_view(m)


@router.delete("/channels/{channel_id}/members/{user_id}", status_code=204)
async def remove_member(
    channel_id: str, user_id: str, user: CurrentUser, session: DbSession
) -> None:
    try:
        await svc.remove_member(
            session, channel_id=channel_id, actor_id=user.id, target_user_id=user_id
        )
    except svc.ChannelNotFound:
        raise HTTPException(404, "channel not found")
    except svc.NotChannelAdmin:
        raise HTTPException(403, "only a channel admin may remove members")
    except svc.NotAMember:
        raise HTTPException(404, "member not found")
    except svc.LastAdmin:
        raise HTTPException(409, "cannot remove the last admin of a channel")


@router.post("/channels/{channel_id}/join", status_code=201)
async def join_channel(channel_id: str, user: CurrentUser, session: DbSession) -> dict:
    try:
        m = await svc.self_join(session, channel_id=channel_id, actor_id=user.id)
    except svc.ChannelNotFound:
        # Both "no such channel" AND "private invite_only channel you can't see"
        # land here — indistinguishable by design (existence-hiding).
        raise HTTPException(404, "channel not found")
    return _member_view(m)


@router.delete("/channels/{channel_id}/leave", status_code=204)
async def leave_channel(channel_id: str, user: CurrentUser, session: DbSession) -> None:
    try:
        await svc.leave(session, channel_id=channel_id, actor_id=user.id)
    except svc.NotAMember:
        # You are not in it (or it doesn't exist / you can't see it) — 404.
        raise HTTPException(404, "channel not found")
    except svc.LastAdmin:
        raise HTTPException(409, "cannot leave as the last admin of a channel")
