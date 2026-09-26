"""Read-position endpoints — *"different devices should see the same thing"*
(Nick's ruling 2026-09-25; #4834a).

TWO NARROW ENDPOINTS, NOT A GENERAL STORE, and the app tab's argument for that is
better than the one this repo started with: the two multi-device consumers have
DIFFERENT CONFLICT SEMANTICS. A read watermark wants version-max; a device label
wants last-write-wins. One general key-value store either lies to one of them or
grows a per-key merge policy — the generality creep that killed design 16. So the
merge rule lives in the endpoint that needs it, and the label (ruling 2) gets its
own when it is built.

THE ISLAND SHIPS FIRST, per CLAUDE.md's silent-desync rule. An unknown wire key is
IGNORED rather than rejected, so an app deployed ahead of this would look healthy
and fail where nobody sees it. These endpoints are additive: a client that never
calls them behaves exactly as it does today, which is what makes shipping first
safe rather than merely conventional.

THE WIRE IS snake_case — `last_read`, NOT `lastRead`. #4834 records the agreed
shape as `{lastRead: ulid, seq: int}`, which is DART FIELD NAMING written in
prose, not a wire spec. Checked against the app's own decoders rather than
inferred: `channel.dart` reads `j['aiko_channel']`, `channel_member.dart` reads
`j['can_post']` and `j['display_name']` — snake_case on the wire, camelCase in
Dart, throughout. Every other island endpoint agrees. This is written down
because it is exactly the silent-desync shape CLAUDE.md warns about: an unknown
key is IGNORED, not rejected, so `lastRead` would decode as null, every watermark
would read as absent, and both sides would look healthy.

AUTHORIZATION IS OWNERSHIP, FULL STOP. Both routes read the caller's own id from
``CurrentUser`` and never accept a user id from the request. There is no path
here that can address another user's positions, so there is no ACL to get wrong —
the strongest form of the "enforce at the backend, through one door" convention,
because the door has only one room behind it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..domain import read_positions_service
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["read-positions"])

# A generous ceiling rather than a tuned one. The client syncs every channel it
# knows about in one request, so the bound exists to stop an unbounded body, not
# to express a product limit; a user in more than this many channels is a
# different conversation than a validation error.
_MAX_POSITIONS = 1000


class ReadPosition(BaseModel):
    """One channel's position.

    ``seq`` is CLIENT-SUPPLIED and monotonic per (user, channel). The island does
    not mint it and could not: it sees network order, not causal order, so a
    server counter would degrade the merge to last-write-wins — exactly the
    semantics ``seq`` exists to prevent."""

    last_read: str = Field(
        max_length=26,
        description="26-char ULID, or '' — the client's first-sight floor for a "
                    "channel that was empty at settle time. '' is a VALUE, not a "
                    "missing field.",
    )
    seq: int = Field(ge=0)


class SetReadPositionsReq(BaseModel):
    positions: dict[str, ReadPosition] = Field(max_length=_MAX_POSITIONS)


@router.get("/read-positions")
async def get_read_positions(user: CurrentUser, session: DbSession) -> dict:
    """The caller's read positions, ``{channel_id: {last_read, seq}}``.

    Returns every stored position, including ones for channels the caller can no
    longer read. A position is a private note about where they got to, not a
    claim about access — dropping it would silently lose a place they can return
    to, while keeping it reveals nothing (they supplied the channel id in the
    first place)."""
    stored = await read_positions_service.get_positions(session, user_id=user.id)
    return {
        "positions": {
            cid: {"last_read": last_read, "seq": seq}
            for cid, (last_read, seq) in stored.items()
        }
    }


@router.put("/read-positions", status_code=status.HTTP_204_NO_CONTENT)
async def set_read_positions(
    req: SetReadPositionsReq, user: CurrentUser, session: DbSession,
) -> None:
    """Merge the caller's positions by ``seq``-max. Idempotent and
    order-independent.

    A PUT rather than a POST because it is a merge into a named resource whose
    identity is the caller — replaying it changes nothing, which is the property
    a multi-device sync needs when two handsets come back online together.

    A batch containing one invalid entry stores NONE of it. A partial store would
    leave the client believing a sync succeeded while some channels silently kept
    an old watermark, which is a worse failure than a refused request."""
    try:
        await read_positions_service.set_positions(
            session,
            user_id=user.id,
            positions={
                cid: (p.last_read, p.seq) for cid, p in req.positions.items()
            },
        )
    except read_positions_service.InvalidReadPosition as exc:
        # 422, matching the boundary's own vocabulary for a well-formed request
        # carrying an unusable value.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
