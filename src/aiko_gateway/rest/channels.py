"""Channel-list endpoint.

I1 (read requires auth): the `CurrentUser` dependency rejects an unauthenticated
caller before any row is read. I2 (membership-scoped visibility, #36): the list
is filtered to the channels the user may see — every public channel plus the
private channels they belong to (`acl.visible_channels`). A private channel the
user is not a member of never appears, so its existence is not leaked.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..domain import acl
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["channels"])


@router.get("/channels")
async def list_channels(user: CurrentUser, session: DbSession) -> dict:
    rows = await acl.visible_channels(session, user.id)
    return {"channels": [
        {"id": c.id, "name": c.name, "kind": c.kind, "aiko_channel": c.aiko_channel}
        for c in rows
    ]}
