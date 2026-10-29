"""Hold a stranger's call invitation until it is too old to ring (design 21).

Nick, 2026-10-29: *withhold, and keep the missed call.* The wake gate
(`push_service._spoken_here`) already refuses a stranger's push, but an OPEN app
received the same invite over its socket and rang it. Withholding is the only
fix that protects every installed app the day the island deploys: a stamp would be
ignored by old apps, and the signed body cannot be rewritten in transit.

THE RULE. A message is held from viewer R while:
  1. it is a call/2 INVITE (`push_service.parse_call_body`, the one parser);
  2. in a DM;
  3. sent by a PERSON (`sender_kind == human`). A non-person needs per-conversation
     consent on the handset, which a stranger cannot have (the app's split: consent
     for non-persons, conduct for persons, 25-TEMPER "What holds");
  4. R is not the sender and R had not posted in the DM before it
     (`push_service.spoke_before`, bounded by the invite's own id);
  5. now < `release_at_ms`.

Conditions 1-4 are facts about rows that already exist, so the hold is
RE-DERIVABLE: nothing records who was held, and the caller is never told (the
no-refused-ring-record ruling, Nick 2026-09-01). Only 5 moves, and it moves one
way: a held row becomes visible, never the reverse.

EVERY FACT IS READ FROM THE ROW. The kind, the channel and the pair come from the
database, never from a caller's word for them: the lesson `push_service._recipients`
records from cage-match #139, where three caller-supplied locks were swept out of
one module.
"""
from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import dm_service, moderation_service, push_service
from .ids import ulid_floor, ulid_ms
from .models import Channel, ChannelKind, Message, SenderKind
from .push_result import WakeKind

# How long past its anchor an invite is held. The app rings an invite with no
# recorded wake only while `-5 s <= app_now - signedAt <= 10 s`
# (`call_invite.dart`: kCallInviteClockSkewTolerance, kCallInviteFreshness), judged
# on the RECIPIENT's clock. 60 s clears the 10 s window with 50 s to spare for that
# clock running behind the island's, and exceeds the 30 s ring ceiling
# (`push_result.RING_CEILING_SECONDS`), so the missed call lands after any ring for
# it would have ended. A fifth call clock; reconciling them is claude-tasks#4233.
HOLD_MS = 60_000

# How far ahead of the island's clock a DM invite may be signed and still be
# accepted. Further than this is refused at ingest (`refuse_future_dated`), and that
# refusal is what makes every holdable row sit inside a primary-key range: without
# it, an invite signed a day ahead would have to be held for a day, outside any
# bounded scan, or released early and ring when history is re-read near its signed
# time. The app already refuses anything more than 5 s in its future, so this only
# turns away calls that could never ring anyone.
FUTURE_CAP_MS = 60_000


class FutureDatedInvite(Exception):
    """A DM call invitation signed more than FUTURE_CAP_MS ahead of the island.
    Raised from the mutator door (`messages_service.create_outbound`); the WS route
    maps it to the existing `bad_origin` error. Refused for EVERY caller, friend or
    stranger, so the refusal says nothing about the recipient's conduct."""


def now_ms() -> int:
    return int(time.time() * 1000)


def is_invite(body: str) -> bool:
    parsed = push_service.parse_call_body(body)
    return parsed is not None and parsed[0] is WakeKind.CALL_INVITE


def release_at_ms(msg: Message) -> int:
    """When `msg` stops being held: max(signature, island receipt) + HOLD_MS.

    ANCHORED ON THE SIGNATURE, because that is what the app measures against. The max
    with receipt covers an unsigned invite (no `origin`) and costs nothing when the
    caller's clock runs slow. Receipt is read off the row's own ULID, the same clock
    the scan floor uses, so the two cannot drift apart.

    `signed_at_ms` is the envelope's CLAIM: the island does not verify signatures. A
    forged one cannot make the app ring (it refuses an unverified origin), and a
    genuine one is the very number the app will judge, so either way this is the
    right number to wait on.
    """
    received = ulid_ms(msg.id)
    signed = msg.origin.get("signed_at_ms") if msg.origin else None
    anchor = max(received, signed) if isinstance(signed, int) else received
    return anchor + HOLD_MS


def refuse_future_dated(*, channel_kind: str, body: str, origin: dict | None,
                        at_ms: int | None = None) -> None:
    """Raise FutureDatedInvite for a DM invite signed beyond FUTURE_CAP_MS."""
    if channel_kind != ChannelKind.DM or origin is None or not is_invite(body):
        return
    signed = origin.get("signed_at_ms")
    if isinstance(signed, int) and signed > (now_ms() if at_ms is None else at_ms) + FUTURE_CAP_MS:
        raise FutureDatedInvite()


def _scan_floor(from_ms: int) -> str:
    """Every row whose hold could still be running at `from_ms` has an id at or above
    this. `release_at <= receipt + FUTURE_CAP_MS + HOLD_MS` once the ingest cap holds,
    so receipt >= from_ms - FUTURE_CAP_MS - HOLD_MS."""
    return ulid_floor(from_ms - FUTURE_CAP_MS - HOLD_MS)


def _holdable(msg: Message, channel: Channel) -> bool:
    """Conditions 1-3, from the row and its channel."""
    return (channel.kind == ChannelKind.DM
            and msg.sender_kind == SenderKind.HUMAN
            and msg.sender_user_id is not None
            and is_invite(msg.body))


async def _strangers(session: AsyncSession, msg: Message, user_ids: list[str]) -> set[str]:
    """Condition 4: of `user_ids`, those (other than the sender) with no post in the
    DM below `msg.id`."""
    others = [u for u in user_ids if u != msg.sender_user_id]
    spoke = await push_service.spoke_before(
        session, channel_id=msg.channel_id, user_ids=others, before_id=msg.id)
    return {u for u in others if u not in spoke}


async def held_viewers(session: AsyncSession, msg: Message, *,
                       at_ms: int | None = None) -> set[str]:
    """Who `msg` is held from right now. Empty for almost every message, at the cost
    of a parse and no query. The live-fanout exclusion at the send door.

    The pair comes from the CANONICAL `dm:` key, not live membership (the same reason
    `create_outbound`'s block gate reads it there). A malformed key yields nobody;
    `create_outbound` already refuses to write into one.
    """
    if not is_invite(msg.body):
        return set()
    at = now_ms() if at_ms is None else at_ms
    if release_at_ms(msg) <= at:
        return set()
    channel = await session.get(Channel, msg.channel_id)
    if channel is None or not _holdable(msg, channel):
        return set()
    peers = dm_service.canonical_peer_ids(channel.aiko_channel, msg.sender_user_id) or []
    return await _strangers(session, msg, peers)


async def held_ids_by_channel(session: AsyncSession, viewer_id: str,
                              channel_ids: list[str], *,
                              at_ms: int | None = None) -> dict[str, set[str]]:
    """{channel_id: ids of messages held from `viewer_id` right now}, for the read
    paths' visibility predicate and the subscribe fence. Channels with nothing held
    are absent.

    One candidate query over a two-minute primary-key range, then the one parser and
    `release_at_ms` refine it in Python: a SQL LIKE could only approximate the call/2
    regex, and an approximation here would decide what a person can see.
    """
    if not channel_ids:
        return {}
    at = now_ms() if at_ms is None else at_ms
    rows = (await session.execute(
        select(Message, Channel)
        .join(Channel, Channel.id == Message.channel_id)
        .where(Message.channel_id.in_(channel_ids),
               Message.id >= _scan_floor(at),
               Channel.kind == ChannelKind.DM,
               Message.sender_kind == SenderKind.HUMAN,
               Message.sender_user_id.is_not(None),
               Message.sender_user_id != viewer_id)
    )).all()
    held: dict[str, set[str]] = defaultdict(set)
    for msg, channel in rows:
        if not _holdable(msg, channel) or release_at_ms(msg) <= at:
            continue
        if await _strangers(session, msg, [viewer_id]):
            held[msg.channel_id].add(msg.id)
    return dict(held)


async def held_ids(session: AsyncSession, viewer_id: str, channel_ids: list[str], *,
                   at_ms: int | None = None) -> set[str]:
    by_channel = await held_ids_by_channel(session, viewer_id, channel_ids, at_ms=at_ms)
    return set().union(*by_channel.values()) if by_channel else set()


async def due_releases(session: AsyncSession, *, after_ms: int,
                       upto_ms: int) -> list[tuple[Message, set[str]]]:
    """Invites whose hold ends in (after_ms, upto_ms], each with the users it was held
    from and may now reach. The release sweeper's input.

    Asked from the DATABASE AND THE CLOCK ONLY, so a hold that spans a restart is found
    again with nothing to re-arm. Visibility is re-checked here: a takedown or a block
    made during the hold wins over the release.
    """
    rows = (await session.execute(
        select(Message, Channel)
        .join(Channel, Channel.id == Message.channel_id)
        .where(Message.id >= _scan_floor(after_ms),
               Channel.kind == ChannelKind.DM,
               Message.sender_kind == SenderKind.HUMAN,
               Message.sender_user_id.is_not(None),
               Message.deleted_at.is_(None))
        .order_by(Message.id)
    )).all()
    due = []
    for msg, channel in rows:
        if not _holdable(msg, channel) or not after_ms < release_at_ms(msg) <= upto_ms:
            continue
        peers = dm_service.canonical_peer_ids(channel.aiko_channel, msg.sender_user_id) or []
        recipients = await _strangers(session, msg, peers)
        recipients -= await moderation_service.blocked_pair_user_ids(
            session, msg.sender_user_id)
        if recipients:
            due.append((msg, recipients))
    return due
