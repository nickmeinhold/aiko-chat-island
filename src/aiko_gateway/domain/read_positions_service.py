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

THE CHANNEL MUST EXIST, AND A MISSING ONE IS SKIPPED, NOT REFUSED. Existence is a
different question from membership: it is what keeps this table bounded by real
data (users x channels) instead of by whatever ids a client invents, since FK is
OFF in prod (ISL-0002) and the declared FK enforces nothing. It is decided INSIDE
the upsert (``INSERT ... SELECT ... FROM channels WHERE id = ?``), so a channel
deleted mid-write cannot leave an orphan; ``hard_delete_channel`` removes the rows
that already exist. An unknown id is SKIPPED rather than refused because a batch
is all-or-nothing: a device still holding a watermark for a channel someone has
since deleted would otherwise have every future sync refused, and both sides
would look healthy. That watermark can display nowhere, so dropping it loses
nothing. A MALFORMED value is still refused: that is a broken client, not a stale
one.
"""
from __future__ import annotations

import datetime as dt

import re

from sqlalchemy import literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, ChannelReadPosition

# A CANONICAL ULID: uppercase Crockford base32, and a FIRST character of 0-7. The
# app checks the alphabet (`channel_read_store.dart:44`) but not the first
# character, and the first character is the one that matters here: 26 chars x 5
# bits is 130 bits for a 128-bit value, so a real ULID's top two bits are zero and
# it starts 0-7. "Z" * 26 is valid Crockford, sorts above every real ULID, and is
# exactly the value that freezes a channel's unread count at zero.
_CANONICAL_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
# SQLite's INTEGER is a signed 64-bit value; anything larger raised OverflowError
# inside the driver (a 500), so it is refused here as a 422 instead.
_MAX_SEQ = 2**63 - 1


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
    # '' stays LEGAL: it is the app's first-sight floor, a real watermark it
    # persists (`isSortableWatermark = ulid.isEmpty || regex`). Refusing it would
    # refuse the whole all-or-nothing batch for any device with one empty channel.
    if last_read != '' and not _CANONICAL_ULID.fullmatch(last_read):
        raise InvalidReadPosition(
            "last_read must be a canonical ULID (uppercase Crockford, first "
            "character 0-7) or the empty floor ''")
    if not 0 <= seq <= _MAX_SEQ:
        raise InvalidReadPosition(f'seq must be between 0 and {_MAX_SEQ}')


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
    rather than a prefix. An entry for a channel that does not exist is skipped
    (see the module docstring), and the rest of the batch is stored.
    """
    for channel_id, (last_read, seq) in positions.items():
        _validate(channel_id, last_read, seq)

    if not positions:
        return

    now = dt.datetime.now(dt.timezone.utc)
    cols = ChannelReadPosition.__table__.c
    for channel_id, (last_read, seq) in positions.items():
        # EXISTENCE IN THE WRITE: the row is selected FROM channels, so a channel
        # that does not exist (or is deleted mid-write) yields no row to insert.
        # A check-then-insert would leave the gap #410 is about.
        source = select(
            literal(user_id, cols.user_id.type),
            Channel.id,
            literal(last_read, cols.last_read.type),
            literal(seq, cols.seq.type),
            literal(now, cols.updated_at.type),
        ).where(Channel.id == channel_id)
        stmt = sqlite_insert(ChannelReadPosition).from_select(
            ['user_id', 'channel_id', 'last_read', 'seq', 'updated_at'], source)
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
