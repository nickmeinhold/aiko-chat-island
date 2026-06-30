"""Community discovery / detail / join / my-communities endpoints (#32, Phase B2).

The directory layer of Option B (nested servers). This router is a THIN HTTP
translation over ``communities_service`` — the trust-boundary rules (the visibility
predicate, the fail-closed join gate, member_count maintenance) all live in the
service (single enforcement source, mirroring ``acl`` / ``memberships_service``).
This layer only maps the service's typed rejections to HTTP and projects rows to
the wire shape.

I1 (auth): every route takes ``CurrentUser`` so an unauthenticated caller is
rejected before any row is read. Existence-hiding (#36, lifted to the community
grain): ``CommunityNotFound`` -> 404, so a private/taken-down community a viewer
cannot see is indistinguishable from a nonexistent one.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..domain import communities_service as svc
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["communities"])


def _directory_item(c) -> dict:
    """The directory projection (handoff shape) — used for discover items, detail,
    and my-communities so the app decodes one community shape everywhere."""
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "icon_url": c.icon_url,
        "visibility": c.visibility,
        "member_count": c.member_count,
        "owner_id": c.owner_id,
        "category": c.category,
        "default_channel_id": c.default_channel_id,
        "last_activity_at": (
            c.last_activity_at.isoformat() if c.last_activity_at else None),
    }


def _channel_item(ch) -> dict:
    """A community channel in the SAME shape ``GET /v1/channels`` returns, so the
    app's existing channel decoder handles both."""
    return {
        "id": ch.id,
        "name": ch.name,
        "kind": ch.kind,
        "aiko_channel": ch.aiko_channel,
        "community_id": ch.community_id,
    }


# NOTE: route registration order matters — the static ``/communities/discover`` and
# exact ``/communities`` paths are declared BEFORE ``/communities/{community_id}``
# so "discover" is never captured as a community id.


@router.get("/communities/discover")
async def discover_communities(
    user: CurrentUser,
    session: DbSession,
    q: str | None = None,
    category: str | None = None,
    sort: str = "members",
    cursor: str | None = None,
) -> dict:
    """The public directory. Cursor-paginated; ``next_cursor`` is null at the end."""
    try:
        rows, next_cursor = await svc.discover(
            session, viewer_id=user.id, q=q, category=category,
            sort=sort, cursor=cursor)
    except svc.InvalidCursor:
        raise HTTPException(400, "invalid cursor")
    return {
        "communities": [_directory_item(c) for c in rows],
        "next_cursor": next_cursor,
    }


@router.get("/communities")
async def list_my_communities(user: CurrentUser, session: DbSession) -> dict:
    """The communities the caller belongs to (replaces the role GET /v1/channels
    plays today, app-side)."""
    rows = await svc.list_mine(session, viewer_id=user.id)
    return {"communities": [_directory_item(c) for c in rows]}


@router.get("/communities/{community_id}")
async def get_community(
    community_id: str, user: CurrentUser, session: DbSession
) -> dict:
    """Detail + the channels in it the caller may see. 404 (fail closed) for a
    community that does not exist or the caller may not see."""
    try:
        community, channels = await svc.community_detail(
            session, viewer_id=user.id, community_id=community_id)
    except svc.CommunityNotFound:
        raise HTTPException(404, "community not found")
    return {
        **_directory_item(community),
        "channels": [_channel_item(ch) for ch in channels],
    }


@router.post("/communities/{community_id}/join", status_code=201)
async def join_community(
    community_id: str, user: CurrentUser, session: DbSession
) -> dict:
    """Join the community; returns it + the channels to subscribe + ``joined``
    (false for an idempotent re-join). 404 (fail closed) if it is no longer
    visible/joinable at join time (concurrent shrink)."""
    try:
        community, channels, joined = await svc.join(
            session, viewer_id=user.id, community_id=community_id)
    except svc.CommunityNotFound:
        raise HTTPException(404, "community not found")
    return {
        "community": _directory_item(community),
        "channels": [_channel_item(ch) for ch in channels],
        "joined": joined,
    }
