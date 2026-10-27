"""Per-user read positions — the island half of *"different devices should see
the same thing"* (Nick, 2026-09-25; #358). The wire contract is stated ONCE, in
PR #429's v2 comment (compare-and-swap); this module implements it.

COMPARE-AND-SWAP, AND WHY. A write says "set this channel's watermark, editing the
version I last saw" (``base_seq``). The island applies it only if that is still
the version it holds, and then stores ``base_seq + 1`` itself. Otherwise nothing
is written, and the reply carries what the island DOES hold. The client never
chooses a version number, which removes the three failures the first design
(the client sending an absolute ``seq``, highest wins) had, each found by a
different family in #429's round 1:

* a write that LOST looked like success, so the losing device kept its own value
  and the devices went on disagreeing (Noether, Tesla);
* two devices sending the same number resolved by arrival order, silently
  (Carnot);
* one bad value near ``2**63`` won once and froze the channel on every device
  forever (Tesla).

THE ISLAND INCREMENTS, BUT IT DOES NOT ORDER. It never claims one device's edit
came after another's (it sees network order, not causal order). It only says
"this is the version I hold". Deciding what a device does on a conflict (adopt
the stored value, or re-send a forward read on top of it) is the app's policy.
MARK-AS-UNREAD is an ordinary edit carrying an earlier ULID; nothing compares
ULIDs here, so it applies like any other.

NO ROW IS VERSION 0. A first write sends ``base_seq = 0``. A client whose
``base_seq`` is ahead of a missing row (the channel's positions were cleared)
gets a conflict at version 0 and resends from there. One rule covers both.

ATOMIC BY CONSTRUCTION. Each entry is ONE conditional statement: an
``UPDATE … WHERE seq = :base`` or an ``INSERT … SELECT … FROM channels … ON
CONFLICT DO NOTHING``. Two of the same user's devices can't interleave a
read-compare-write, and on prod SQLite ``SELECT … FOR UPDATE`` is inert anyway
(`reference_sqlite_concurrency_dual_mechanism`). The outcome is read back inside
the same transaction, which under SQLite's single writer is the state the write
produced.

THE CHANNEL MUST EXIST, AND A MISSING ONE IS SKIPPED, NOT REFUSED. Existence is a
different question from membership: it is what keeps this table bounded by real
data (users x channels) instead of by whatever ids a client invents, since FK is
OFF in prod (ISL-0002). The insert selects FROM ``channels``, so a missing channel
produces no row, and ``hard_delete_channel`` clears the rows that exist. Skipped,
not refused, because malformed entries refuse the whole batch: a device holding a
watermark for a since-deleted channel would otherwise never sync again. A
MALFORMED value is still refused (a broken client, not a stale one).

MEMBERSHIP IS NOT CHECKED HERE, and that is a deliberate boundary rather than an
omission. A read position is a private per-user note about a channel id; it
grants nothing, reveals nothing about the channel, and is only ever readable by
its owner. Gating it on membership would mean a user who leaves a channel loses
their place in it — and rejoining would silently resume from a stale watermark.
The ACL that matters is on the MESSAGES, which is where it already is.

DIALECT-SPECIFIC ON PURPOSE. ``sqlite.insert`` is used because SQLite is the sole
engine in dev AND prod (CLAUDE.md, and the same reasoning ``domain/types.py``
records for ``UtcDateTime``). An island that moves to Postgres must revisit this
file (and widen ``seq`` to BIGINT) AND that one.
"""
from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass

from sqlalchemy import and_, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, ChannelReadPosition

# A CANONICAL ULID: uppercase Crockford base32, and a FIRST character of 0-7. 26
# chars x 5 bits is 130 bits for a 128-bit value, so a real ULID's top two bits are
# zero and it starts 0-7. "Z" * 26 is valid Crockford (so the app's alphabet-only
# check accepts it, aiko_chat_app#431) but sorts above every real ULID, which
# freezes a channel's unread count at zero.
_CANONICAL_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
# SQLite's INTEGER is signed 64-bit. The island stores base_seq + 1, so the
# largest base it can accept is one below that.
_MAX_SEQ = 2**63 - 1
MAX_BASE_SEQ = _MAX_SEQ - 1


class Outcome(enum.StrEnum):
    """What happened to one entry. Closed set; the strings are the wire values."""
    APPLIED = "applied"    # the base matched; stored at base_seq + 1
    CONFLICT = "conflict"  # the base did not match; nothing written
    SKIPPED = "skipped"    # the channel does not exist; nothing stored


@dataclass(frozen=True)
class EntryResult:
    """One entry's outcome and, unless SKIPPED, the value the island now holds.

    On CONFLICT ``last_read`` is None and ``seq`` is 0 when the island holds no
    position for that channel: version 0, from which the client can resend."""
    outcome: Outcome
    last_read: str | None = None
    seq: int | None = None


class InvalidReadPosition(Exception):
    """A position the island refuses to store, naming the channel it is about.

    Named rather than a bare ValueError for the reason `oauth.UnknownProvider`
    and `moderation_service.UnknownReportReason` are: the route maps it to a 422,
    and an in-process caller gets a refusal it can catch by type rather than a
    500. It carries ``channel_id`` so a client can find the one bad entry."""

    def __init__(self, channel_id: str, why: str) -> None:
        super().__init__(f"read position for channel {channel_id!r}: {why}")
        self.channel_id = channel_id


def _validate(channel_id: str, last_read: str, base_seq: int) -> None:
    """Refuse at the WRITE, the same posture `UtcDateTime` takes on naive datetimes.

    A malformed ``last_read`` is not cosmetic: the app sorts watermarks
    lexicographically, so a value above every real ULID would pin a bad floor and
    freeze that channel's unread count at zero. The app checks at its own
    boundary too; a store that only the client validates is a store the client
    validates.
    """
    if not channel_id:
        raise InvalidReadPosition(channel_id, "channel_id must not be empty")
    # '' stays LEGAL: it is the app's first-sight floor, a real watermark it
    # persists (`isSortableWatermark = ulid.isEmpty || regex`). Refusing it would
    # refuse the whole batch for any device with one empty channel.
    if last_read != '' and not _CANONICAL_ULID.fullmatch(last_read):
        raise InvalidReadPosition(
            channel_id, "last_read must be a canonical ULID (uppercase "
            "Crockford, first character 0-7) or the empty floor ''")
    if not 0 <= base_seq <= MAX_BASE_SEQ:
        raise InvalidReadPosition(
            channel_id, f"base_seq must be between 0 and {MAX_BASE_SEQ}")


async def _write(session: AsyncSession, *, user_id: str, channel_id: str,
                 last_read: str, base_seq: int, now: dt.datetime) -> None:
    """The one conditional statement for one entry. Its effect is read back by
    the caller, so this returns nothing: the stored row is the answer."""
    if base_seq == 0:
        # A first write: only where the channel exists (the row is selected FROM
        # channels), and only if no row exists yet (DO NOTHING is the conflict).
        cols = ChannelReadPosition.__table__.c
        source = select(
            literal(user_id, cols.user_id.type),
            Channel.id,
            literal(last_read, cols.last_read.type),
            literal(1, cols.seq.type),
            literal(now, cols.updated_at.type),
        ).where(Channel.id == channel_id)
        stmt = sqlite_insert(ChannelReadPosition).from_select(
            ['user_id', 'channel_id', 'last_read', 'seq', 'updated_at'], source
        ).on_conflict_do_nothing(
            index_elements=[ChannelReadPosition.user_id,
                            ChannelReadPosition.channel_id])
    else:
        # THE SWAP: only if the stored version is still the one the client saw.
        stmt = update(ChannelReadPosition).where(
            ChannelReadPosition.user_id == user_id,
            ChannelReadPosition.channel_id == channel_id,
            ChannelReadPosition.seq == base_seq,
        ).values(last_read=last_read, seq=base_seq + 1, updated_at=now)
    await session.execute(stmt)


async def set_positions(
    session: AsyncSession,
    *,
    user_id: str,
    positions: dict[str, tuple[str, int]],
) -> dict[str, EntryResult]:
    """Apply ``{channel_id: (last_read, base_seq)}`` by compare-and-swap, and
    return each entry's outcome with the value the island now holds.

    Every entry is validated BEFORE anything is written, so a malformed entry
    (raised as InvalidReadPosition, naming its channel) stores none of the batch.
    Commits: the route is the only caller, and the reply must describe durable
    state.
    """
    for channel_id, (last_read, base_seq) in positions.items():
        _validate(channel_id, last_read, base_seq)
    if not positions:
        return {}

    now = dt.datetime.now(dt.timezone.utc)
    results: dict[str, EntryResult] = {}
    for channel_id, (last_read, base_seq) in positions.items():
        await _write(session, user_id=user_id, channel_id=channel_id,
                     last_read=last_read, base_seq=base_seq, now=now)
        # Read the outcome back: does the channel exist, and what is held now?
        row = (await session.execute(
            select(Channel.id, ChannelReadPosition.last_read,
                   ChannelReadPosition.seq)
            .outerjoin(ChannelReadPosition, and_(
                ChannelReadPosition.channel_id == Channel.id,
                ChannelReadPosition.user_id == user_id))
            .where(Channel.id == channel_id)
        )).one_or_none()
        if row is None:
            results[channel_id] = EntryResult(Outcome.SKIPPED)
        elif row.seq == base_seq + 1 and row.last_read == last_read:
            results[channel_id] = EntryResult(Outcome.APPLIED, row.last_read, row.seq)
        else:
            results[channel_id] = EntryResult(
                Outcome.CONFLICT, row.last_read, row.seq or 0)
    await session.commit()
    return results


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
