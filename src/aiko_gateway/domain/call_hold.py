"""Hold a stranger's call invitation until it is too old to ring (design 21).

Nick, 2026-10-29: *withhold, and keep the missed call.* The wake gate
(`push_service._spoken_here`) already refuses a stranger's push, but an OPEN app
received the same invite over its socket and rang it. Withholding is the only
fix that protects every installed app the day the island deploys: a stamp would be
ignored by old apps, and the signed body cannot be rewritten in transit.

THE RULE. A message is held from viewer R while:
  1. it is a call/2 INVITE (`INVITE_GLOB`, pinned to `push_service.parse_call_body`);
  2. in a DM;
  3. sent by a PERSON (`sender_kind == human`). A non-person needs per-conversation
     consent on the handset, which a stranger cannot have (the app's split: consent
     for non-persons, conduct for persons, 25-TEMPER "What holds");
  4. R is not the sender and R had not posted in the DM before it
     (`push_service.prior_post`, the one definition the wake gate also asks);
  5. now < max(signed_at, created_at) + HOLD_MS, all three on the island's WALL clock.

ONE SQL CLAUSE, EVALUATED BY THE STATEMENT THAT RETURNS THE ROW (`held_clause`).
Round 1 of cage-match #445 computed the held set in one SELECT and applied it in the
next. Under pysqlite each SELECT is its own read, so an invite committed between them
was absent from the set and present in the rows, and REST history served it fresh
(Carnot; measured red by `test_history_never_serves_an_invite_committed_between_its_
statements`). This repo's rule for writes ("fold the predicate into the statement, do
not observe-then-act") holds for a read that is itself the boundary. The send-path
exclusion asks the same clause, so there is one definition of "held", not a SQL one
and a Python one that could drift.

TWO INSTRUMENTS, EACH ASKED ONE QUESTION (design 21 recast). Time (is this still
ringable? is this signature too far ahead?) is the island's raw wall clock against
wall-clock readings (`signed_at_ms`, `created_at`), because that is what the app does
with the recipient's wall clock. Ordering (did R post before this?) is the monotonic
message axis (`ids.next_axis_ulid`), and the hold never reads time off an id. Fusing
the two produced every clock finding in #445 rounds 1-3 and, in the recast's first
draft, an outage of calling (21-TEMPER.md). The hold is therefore exactly as good as
the island's wall clock against the recipient's, within 50 s: the residual Nick
accepted 2026-10-29.

Conditions 1-4 are facts about rows that already exist, so the hold is RE-DERIVABLE:
nothing records who was held, and the caller is never told (the no-refused-ring-record
ruling, Nick 2026-09-01). A held invite is RELEASED by time alone and reaches each
device on its next history walk; there is no release push (Nick, 2026-10-29: the
sweeper produced four defects, and the capped subscribe fence makes its absence
lossless).

EVERY FACT IS READ FROM THE ROW. The kind, the channel and the pair come from the
database, never from a caller's word for them: the lesson `push_service._recipients`
records from cage-match #139.
"""
from __future__ import annotations

import re

import datetime as dt
import time

from sqlalchemy import and_, case, exists, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from . import dm_service, push_service
from .models import Channel, ChannelKind, Membership, Message, SenderKind
from .push_result import CALL_ID_PATTERN, WakeKind

# How long past its anchor an invite is held. The app rings an invite with no
# recorded wake only while `-5 s <= app_now - signedAt <= 10 s`
# (`call_invite.dart`: kCallInviteClockSkewTolerance, kCallInviteFreshness), judged
# on the RECIPIENT's clock. 60 s clears the 10 s window with 50 s to spare for that
# clock running behind the island's, and exceeds the 30 s ring ceiling
# (`push_result.RING_CEILING_SECONDS`), so the missed call lands after any ring for
# it would have ended. A fifth call clock; reconciling them is claude-tasks#4233.
HOLD_MS = 60_000

# How far ahead of the island's clock a DM invite may be signed and still be
# accepted. Further than this is refused at ingest (`refuse_future_dated`), which
# bounds how long any hold lasts (max(signed, created_at) + HOLD_MS <= created_at +
# 2 min, on a wall clock that doesn't step). Without it, an invite signed a day ahead
# would be held for a day, and for that day the recipient's subscribe fence would stay
# capped below it and every subscribe would replay everything after it. The app
# already refuses anything more than 5 s in its future, so this only turns away calls
# that could never ring anyone.
FUTURE_CAP_MS = 60_000


def _glob_for(pattern: str) -> str:
    """`CALL_ID_PATTERN` as a SQLite GLOB: two character classes and a repeat count.
    Refuses any other shape at import, so a change to the id pattern cannot leave
    the SQL quietly matching the old one."""
    m = re.fullmatch(r"(\[[^\]]+\])(\[[^\]]+\])\{(\d+)\}", pattern)
    if m is None:
        raise RuntimeError(f"CALL_ID_PATTERN changed shape ({pattern!r}); re-derive INVITE_GLOB")
    return m[1] + m[2] * int(m[3])


# The call/2 invite body as a GLOB: case-sensitive, whole-string, with the id's
# character classes, so it accepts exactly what `parse_call_body` reads as an
# invite. Pinned against the parser by `test_the_glob_is_the_parser`.
INVITE_GLOB = f"aiko:call/2 {_glob_for(CALL_ID_PATTERN)} \u00b7 \U0001F4DE started a call"

def now_ms() -> int:
    """The hold's clock: the island's RAW wall clock, the same clock `created_at` was
    read from and the kind of clock `signed_at_ms` and the app's own check use.

    Not a high-water mark, deliberately. A high-water mark freezes ahead of real time
    after a step that CORRECTS a fast clock, and every invite received during the
    freeze would be judged against a "now" from the future and released while still
    ringable: a correcting step turned into a ring window. The cost of the raw clock is
    Tesla's #445 r1 finding: a backward step can make a just-released invite held
    again for at most the step. It can't ring anyone, and it can't strand the missed
    call either: a walk inside that window would otherwise return later rows without
    the re-held invite and set the device's watermark past it (Tesla + Carnot, #445
    recast rounds 1-2), so forward history pages stop below any invite held from the
    viewer (`messages_service.get_history`). The re-hold only delays it."""
    return int(time.time() * 1000)


def _instant(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


class FutureDatedInvite(Exception):
    """A DM call invitation signed more than FUTURE_CAP_MS ahead of the island.
    Raised from the mutator door (`messages_service.create_outbound`); the WS route
    maps it to the existing `bad_origin` error. Refused for EVERY caller, friend or
    stranger, so the refusal says nothing about the recipient's conduct."""


def is_invite(body: str) -> bool:
    parsed = push_service.parse_call_body(body)
    return parsed is not None and parsed[0] is WakeKind.CALL_INVITE


def refuse_future_dated(*, channel_kind: push_service.ChannelKindStr, body: str,
                        origin: dict | None, at_ms: int | None = None) -> None:
    """Raise FutureDatedInvite for a DM invite signed beyond FUTURE_CAP_MS."""
    if channel_kind != ChannelKind.DM or origin is None or not is_invite(body):
        return
    signed = origin.get("signed_at_ms")
    # Fail CLOSED on a non-int: `held_clause` casts the stored value to an integer,
    # so a value this check skipped would still anchor the hold, past the two-minute
    # bound the cap exists to keep (Noether, #445 r2). `signing.validate_origin`
    # already refuses one at the WS door; this keeps the two readers of the field in
    # agreement whatever writer comes next.
    if (not isinstance(signed, int) or isinstance(signed, bool)
            or signed > (now_ms() if at_ms is None else at_ms) + FUTURE_CAP_MS):
        raise FutureDatedInvite()


def held_clause(m, viewer_id: str, at_ms: int):
    """SQL: the row `m` (Message or an alias of it) is held from `viewer_id` at `at_ms`.

    Every term reads `m`'s own columns or a correlated subquery on them, so the
    statement that selects `m` decides its hold from the same snapshot it reads `m`
    from. The time term reads `created_at` and the signature, both wall-clock
    readings, and NEVER the id: the id is the ordering axis, and its timestamp is a
    sort key that may run ahead of the wall (design 21 recast, class 2).

    NO PRIMARY-KEY FLOOR HERE. Round 2 carried `id >= ulid_floor(now - HOLD - CAP)` as
    an index bound, a second answer to the question the time term answers, and it
    failed OPEN: whenever it disagreed (a detuned clock, a row the cap never saw) it
    released a still-ringable invite (Tesla, #445 r2). The cheap terms lead instead
    (SQLite evaluates AND left to right), so an ordinary message stops at the GLOB.
    """
    c = aliased(Channel)
    signed = func.coalesce(m.origin["signed_at_ms"].as_integer(), 0)
    window = at_ms - HOLD_MS
    return and_(
        m.body.op("GLOB")(INVITE_GLOB),
        m.sender_kind == SenderKind.HUMAN,
        m.sender_user_id.is_not(None),
        m.sender_user_id != viewer_id,
        exists().where(c.id == m.channel_id, c.kind == ChannelKind.DM),
        # Condition 5: max(signed, created_at) + HOLD > now.
        or_(m.created_at > _instant(window), signed > window),
        ~push_service.prior_post(channel_id=m.channel_id, author_id=viewer_id,
                                 before_id=m.id),
    )


def not_held(m, viewer_id: str, at_ms: int):
    """The visibility half: CASE, not NOT, so a NULL anywhere in the clause reads as
    "not held" (visible) rather than three-valued NULL, which a WHERE drops."""
    return case((held_clause(m, viewer_id, at_ms), false()), else_=true())


async def _held(session: AsyncSession, msg_id: str, viewer_id: str, at_ms: int) -> bool:
    return bool(await session.scalar(
        select(func.count()).select_from(Message)
        .where(Message.id == msg_id, held_clause(Message, viewer_id, at_ms))))


async def held_viewers(session: AsyncSession, msg: Message, *,
                       at_ms: int | None = None) -> set[str]:
    """Who `msg` is held from right now: the live-fanout exclusion at the send door.
    Asks `held_clause` per candidate, so it is the same definition the reads use. A
    non-invite costs a parse and no query.

    Candidates are the CANONICAL `dm:` pair, not live membership (the same reason
    `create_outbound`'s block gate reads it there). If the key does not parse, the
    candidates are the channel's raw `Membership` rows instead: an empty answer there
    would read as "nobody is held" and the live door would deliver what every read door
    holds (Noether, #445 r3). `create_outbound` refuses to write into such a channel,
    so this branch is for whatever writer comes next.
    """
    if not is_invite(msg.body) or msg.sender_user_id is None:
        return set()
    at = now_ms() if at_ms is None else at_ms
    channel = await session.get(Channel, msg.channel_id)
    if channel is None or channel.kind != ChannelKind.DM:
        return set()
    peers = dm_service.canonical_peer_ids(channel.aiko_channel, msg.sender_user_id)
    if peers is None:
        peers = list((await session.execute(
            select(Membership.user_id).where(Membership.channel_id == msg.channel_id)
        )).scalars())
    return {p for p in peers if await _held(session, msg.id, p, at)}

