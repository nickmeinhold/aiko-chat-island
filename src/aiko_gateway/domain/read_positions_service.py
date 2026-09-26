"""Per-user read positions — the island half of *"different devices should see
the same thing"* (Nick, 2026-09-25; #4834a).

THE MERGE RULE, AND WHY IT IS NOT MAX-ON-ULID. Keeping the greatest ``last_read``
is the obvious rule and it makes MARK-AS-UNREAD IMPOSSIBLE: moving a watermark
backwards writes a lower ULID, which always loses to the stored one. The app tab
proposed that first, Nick rejected it, and they corrected to a client-supplied
monotonic ``seq``. So: **``seq`` is compared, ``last_read`` is carried.** A write
whose ``seq`` is not strictly greater than the stored one is a no-op.

WHY THE CLIENT OWNS THE COUNTER. The island cannot order two devices' edits — it
sees them arrive in network order, which is not causal order. A server-minted
counter would make the last writer win, which is exactly the semantics the ``seq``
exists to avoid. The island's job is to hold the maximum, not to decide it.

ATOMIC BY CONSTRUCTION, not by care. The rule is folded INTO the write as a
single ``INSERT ... ON CONFLICT DO UPDATE ... WHERE excluded.seq > stored.seq``.
A read-then-compare-then-write would be a TOCTOU between two of the SAME user's
devices syncing at once — and on prod SQLite ``SELECT ... FOR UPDATE`` is INERT
(`reference_sqlite_concurrency_dual_mechanism`), so the lock a reader might reach
for does not exist. CLAUDE.md states the general form: fold the predicate into
the write, never observe-then-write.

DIALECT-SPECIFIC ON PURPOSE. ``sqlite.insert`` is used rather than a portable
two-statement dance because SQLite is the sole engine in dev AND prod (CLAUDE.md,
and the same reasoning ``domain/types.py`` records for ``UtcDateTime``). An island
that moves to Postgres must revisit this file AND that one; both say so rather
than pretending to a portability neither has.

MEMBERSHIP IS NOT CHECKED HERE, and that is a deliberate boundary rather than an
omission. A read position is a private per-user note about a channel id; it
grants nothing, reveals nothing about the channel, and is only ever readable by
its owner. Gating it on membership would mean a user who leaves a channel loses
their place in it — and rejoining would silently resume from a stale watermark.
The ACL that matters is on the MESSAGES, which is where it already is.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChannelReadPosition

# A ULID is 26 chars; '' is the app's documented first-sight floor for a channel
# that was empty at settle time (`channel_read_store.dart`), not a missing value.
_ULID_LEN = 26


class InvalidReadPosition(Exception):
    """A position the island refuses to store.

    Named rather than a bare ValueError for the reason `oauth.UnknownProvider`
    and `moderation_service.UnknownReportReason` are: the route maps it to a 422,
    and an in-process caller gets a refusal it can catch by type rather than a
    500."""


def _validate(channel_id: str, last_read: str, seq: int) -> None:
    """Refuse at the WRITE, the same posture `UtcDateTime` takes on naive datetimes.

    A malformed ``last_read`` is not cosmetic: the app sorts watermarks
    lexicographically, so a value above every real ULID would pin a bad monotonic
    floor and freeze that channel's unread count at zero forever. The app rejects
    it at its own boundary too (cage-match #109); this is the other end of the
    same refusal, because a store that only the client validates is a store the
    client validates.
    """
    if not channel_id:
        raise InvalidReadPosition('channel_id must not be empty')
    if last_read != '' and len(last_read) != _ULID_LEN:
        raise InvalidReadPosition(
            f'last_read must be a 26-char ULID or the empty floor, got '
            f'{len(last_read)} chars')
    if not last_read.isalnum() and last_read != '':
        raise InvalidReadPosition('last_read must be alphanumeric (ULID)')
    if seq < 0:
        raise InvalidReadPosition('seq must be >= 0')


async def set_positions(
    session: AsyncSession,
    *,
    user_id: str,
    positions: dict[str, tuple[str, int]],
) -> None:
    """Merge ``{channel_id: (last_read, seq)}`` into this user's stored positions.

    Idempotent, and order-independent across devices: replaying an older device's
    write after a newer one changes nothing, because the ``WHERE`` clause on the
    upsert refuses a non-increasing ``seq``. Every position is validated BEFORE
    anything is written, so a batch containing one bad entry stores none of it
    rather than a prefix.
    """
    for channel_id, (last_read, seq) in positions.items():
        _validate(channel_id, last_read, seq)

    if not positions:
        return

    now = dt.datetime.now(dt.timezone.utc)
    for channel_id, (last_read, seq) in positions.items():
        stmt = sqlite_insert(ChannelReadPosition).values(
            user_id=user_id,
            channel_id=channel_id,
            last_read=last_read,
            seq=seq,
            updated_at=now,
        )
        # THE MERGE RULE, in the statement rather than around it. Without the
        # WHERE this is last-write-wins and the seq is decoration.
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                ChannelReadPosition.user_id,
                ChannelReadPosition.channel_id,
            ],
            set_={
                'last_read': stmt.excluded.last_read,
                'seq': stmt.excluded.seq,
                'updated_at': stmt.excluded.updated_at,
            },
            where=stmt.excluded.seq > ChannelReadPosition.seq,
        )
        await session.execute(stmt)
    await session.commit()


async def get_positions(
    session: AsyncSession, *, user_id: str
) -> dict[str, tuple[str, int]]:
    """This user's read positions, as ``{channel_id: (last_read, seq)}``.

    Returns every stored position rather than only those for channels the user
    can currently read — see the module docstring's boundary note. A position for
    a channel the client no longer knows about is inert on the client, whereas
    dropping it here would silently lose a place the user could return to.
    """
    rows = (await session.execute(
        select(
            ChannelReadPosition.channel_id,
            ChannelReadPosition.last_read,
            ChannelReadPosition.seq,
        ).where(ChannelReadPosition.user_id == user_id)
    )).all()
    return {r.channel_id: (r.last_read, r.seq) for r in rows}
