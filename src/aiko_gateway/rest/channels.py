"""Channel-list endpoint.

I1 (read requires auth): the `CurrentUser` dependency is the enforcement point —
an unauthenticated caller is rejected before any row is read. I2 (membership-
scoped visibility) is deferred to Phase 2's ACL suite (plan §A3); for now every
authenticated user sees every channel. See the I2 follow-up task.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ..domain.models import Channel
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["channels"])


@router.get("/channels")
async def list_channels(user: CurrentUser, session: DbSession) -> dict:
    rows = list((await session.execute(select(Channel))).scalars())
    return {"channels": [
        {"id": c.id, "name": c.name, "kind": c.kind, "aiko_channel": c.aiko_channel}
        for c in rows
    ]}
