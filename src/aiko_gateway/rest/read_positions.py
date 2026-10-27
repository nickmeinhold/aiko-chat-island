"""Read-position endpoints — *"different devices should see the same thing"*
(Nick's ruling 2026-09-25; #4834a).

TWO NARROW ENDPOINTS, NOT A GENERAL STORE, and the app tab's argument for that is
better than the one this repo started with: the two multi-device consumers have
DIFFERENT CONFLICT SEMANTICS. A read watermark wants version-max; a device label
wants last-write-wins. One general key-value store either lies to one of them or
grows a per-key merge policy — the generality creep that killed design 16. So the
conflict rule lives in the endpoint that needs it, and the label (ruling 2) gets its
own when it is built.

THE ISLAND SHIPS FIRST, per CLAUDE.md's silent-desync rule. An unknown wire key is
IGNORED rather than rejected, so an app deployed ahead of this would look healthy
and fail where nobody sees it. These endpoints are additive: a client that never
calls them behaves exactly as it does today, which is what makes shipping first
safe rather than merely conventional.

THE WIRE IS snake_case. A PUT entry is `{last_read, base_seq}`; GET and the PUT
reply carry `{last_read, seq}` (the island's version). The contract is PR #429's
v2 comment. snake_case is checked against the app's own decoders, not inferred:
`channel.dart` reads `j['aiko_channel']`, `channel_member.dart` reads
`j['can_post']`. An unknown key is IGNORED, not rejected, so a camelCase key
would decode as null and every watermark would read as absent, with both sides
looking healthy.

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
from ..domain.rate_limit import rate_limit
from .deps import CurrentUser, DbSession, rate_limit_user

router = APIRouter(prefix="/v1", tags=["read-positions"])

# A generous ceiling rather than a tuned one. The client syncs every channel it
# knows about in one request, so the bound exists to stop an unbounded body, not
# to express a product limit; a user in more than this many channels is a
# different conversation than a validation error.
_MAX_POSITIONS = 1000


class ReadPositionWrite(BaseModel):
    """One channel's write: a watermark, and the version the client last saw.

    ``base_seq`` is NOT a version the client chooses. It names the version the
    client is editing (0 = it has never seen one), and the island applies the
    write only if that is still the version it holds (compare-and-swap; the
    contract is PR #429's v2 comment). The field is deliberately not called
    ``seq``: an unknown key is ignored, not rejected, so a body shaped for the
    first design (``{last_read, seq}``) is a loud 422 for the missing
    ``base_seq`` rather than a silent misreading."""

    last_read: str = Field(
        max_length=26,
        description="Canonical ULID, or '' — the client's first-sight floor for a "
                    "channel that was empty at settle time. '' is a VALUE, not a "
                    "missing field.",
    )
    base_seq: int = Field(ge=0, le=read_positions_service.MAX_BASE_SEQ)


class SetReadPositionsReq(BaseModel):
    positions: dict[str, ReadPositionWrite] = Field(max_length=_MAX_POSITIONS)


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


# RATE-LIMITED, unlike GET: each PUT can carry 1000 entries and runs them inside one
# write transaction, and prod SQLite has ONE writer, so an unthrottled loop would
# queue every message persist behind it (Maxwell, #429 r1). Keyed on the USER, as
# call occupancy learned (#3159): a per-IP budget alone makes everyone behind one
# NAT share it. 60 per 60s per user: a client batches every channel into one PUT,
# so syncing on each channel switch at one switch a second fits for one device,
# and two of the same user's devices at a more realistic rate fit together. The
# per-IP bound only stops one address cycling through many accounts.
@router.put("/read-positions",
            dependencies=[
                rate_limit_user("read_positions_user", limit=60, window=60),
                rate_limit("read_positions", limit=600, window=60),
            ])
async def set_read_positions(
    req: SetReadPositionsReq, user: CurrentUser, session: DbSession,
) -> dict:
    """Apply the caller's writes by compare-and-swap; reply per channel.

    200 with ``{"positions": {channel_id: {"status", "last_read", "seq"}}}``.
    ``status`` is ``applied``, ``conflict`` or ``skipped``. On ``applied`` and
    ``conflict`` the pair is the value the island NOW holds, so a device that lost
    learns the winner in the same round trip instead of believing it synced (the
    first design's 204 hid that). On a conflict with nothing stored, ``seq`` is 0
    and ``last_read`` is null: resend from base 0. ``skipped`` (no such channel)
    carries neither.

    A batch containing one MALFORMED entry stores none of it, and the 422 names
    the channel, so the client can find and drop the bad entry."""
    try:
        results = await read_positions_service.set_positions(
            session,
            user_id=user.id,
            positions={
                cid: (p.last_read, p.base_seq) for cid, p in req.positions.items()
            },
        )
    except read_positions_service.InvalidReadPosition as exc:
        # 422, matching the boundary's own vocabulary for a well-formed request
        # carrying an unusable value.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    out: dict[str, dict] = {}
    for cid, r in results.items():
        entry: dict = {"status": r.outcome.value}
        if r.outcome is not read_positions_service.Outcome.SKIPPED:
            entry["last_read"] = r.last_read
            entry["seq"] = r.seq
        out[cid] = entry
    return {"positions": out}
