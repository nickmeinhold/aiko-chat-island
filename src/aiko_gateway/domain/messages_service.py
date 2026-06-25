"""Message persistence (Phase 1 subset).

Right now the gateway persists messages it observes ON the bus (the canonical
timeline; the gateway's ULID at ingest is the ordering key — plan §A5). The
authenticated send-then-persist path + echo suppression land in the next slice;
until then there is a single writer (ingest), so no double-write to dedupe.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..aiko.payload import InboundMessage
from . import channels_service
from .ids import new_ulid
from .models import Channel, Message, User


def message_view(m: Message) -> dict:
    """The stable MessageView the client contract exposes (plan §A1)."""
    return {
        "msg_id": m.id,
        "channel_id": m.channel_id,
        "sender": {"user_id": m.sender_user_id, "kind": m.sender_kind, "label": m.sender_label},
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        "reply_to": m.reply_to,
    }


async def create_outbound(
    session: AsyncSession, *, user: User, channel: Channel,
    body: str, client_msg_id: str, reply_to: str | None = None,
) -> tuple[Message, bool]:
    """Persist a user's outgoing message (server ULID, server-derived sender —
    invariant I5). Idempotent on (channel, client_msg_id): a resend returns the
    existing row. Returns (row, created)."""
    existing = (await session.execute(
        select(Message).where(
            Message.channel_id == channel.id,
            Message.client_msg_id == client_msg_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = Message(
        id=new_ulid(),
        channel_id=channel.id,
        sender_user_id=user.id,
        sender_kind="human",
        sender_label=user.display_name,
        body=body,
        reply_to=reply_to,
        client_msg_id=client_msg_id,
        aiko_origin=False,
    )
    session.add(row)
    await session.commit()
    return row, True


def _kind_for(channel: Channel, sender_user: User | None) -> str:
    if sender_user is not None:
        return "human"
    if channel.kind in ("llm", "robot"):
        return channel.kind
    return "actor"  # external REPL / unknown bus participant


async def persist_inbound(session: AsyncSession, msg: InboundMessage) -> Message | None:
    """Persist a bus message into its channel. Returns the row, or None if the
    message carries no channel.

    Channel resolution: an inbound bus message is HyperSpace-confirmed evidence
    that its channel exists canonically (ChatServer only relays channels it
    hosts), so a not-yet-reconciled channel is upserted here rather than dropped.
    This closes the startup window between bus discovery and the first
    `channel_list` EC reconcile event, now that `_seed_channels` is retired
    (#1281 incr 2). It is NOT independent seeding/drift — the channel set seen on
    the bus is a subset of HyperSpace's canonical set. Single creation path:
    `channels_service.upsert_channel`."""
    if not msg.channel:
        return None
    channel = await channels_service.upsert_channel(session, msg.channel)

    sender_user = None
    if msg.username:
        sender_user = (await session.execute(
            select(User).where(User.aiko_username == msg.username)
        )).scalar_one_or_none()

    created = (
        dt.datetime.fromtimestamp(msg.timestamp, dt.timezone.utc)
        if msg.timestamp else dt.datetime.now(dt.timezone.utc)
    )
    row = Message(
        id=new_ulid(),
        channel_id=channel.id,
        sender_user_id=sender_user.id if sender_user else None,
        sender_kind=_kind_for(channel, sender_user),
        sender_label=msg.username,
        body=msg.message,
        aiko_origin=True,
        created_at=created,
    )
    session.add(row)
    await session.commit()
    return row


async def latest_ulid(session: AsyncSession, channel_id: str) -> str:
    """The newest *visible* message id in a channel — the live/history *fence*
    a `suback` carries (design 04 §Gap 2). Returns ``""`` for a channel with no
    visible messages: an empty fence means "no history boundary, everything is
    forward/live".

    The ``deleted_at IS NULL`` filter MUST match ``get_history`` exactly: the
    fence and the history pager are two reads of the same id axis, and B4's
    reconnect loop pages history "until cursor >= fence", treating an empty page
    while ``cursor < fence`` as an invariant violation (design 04 round 5). If the
    fence could point past the newest visible row (a soft-deleted tail), that
    termination condition would be unreachable by visible rows and the violation
    check would false-positive. One predicate, both reads — the partition stays
    clean and the invariant stays assertable.
    """
    result = await session.execute(
        select(func.max(Message.id)).where(
            Message.channel_id == channel_id, Message.deleted_at.is_(None)
        )
    )
    return result.scalar_one() or ""


async def get_history(
    session: AsyncSession,
    channel_id: str,
    *,
    before: str | None = None,
    after: str | None = None,
    limit: int,
) -> list[Message]:
    """A page of messages in a channel, **always returned ascending** (oldest
    first) for display. Two cursor directions, mutually exclusive — a ULID is a
    total order, so both walk the same axis:

    * ``before`` (backward, the default — UI scroll-up): the ``limit`` newest
      messages with ``id < before``. Used to load older history a page at a time.
    * ``after`` (forward — B4 reconnect catch-up): the ``limit`` oldest messages
      with ``id > after``. Forward paging fills the oldest gap first, which is
      what makes ``MAX(serverUlid)`` a crash-resumable watermark on the client
      (design 04 §Gap 2). ``after`` wins if both are passed.
    """
    stmt = select(Message).where(
        Message.channel_id == channel_id, Message.deleted_at.is_(None)
    )
    if after is not None:
        # Forward: oldest-above-cursor first; already ascending, no reverse.
        stmt = stmt.where(Message.id > after).order_by(Message.id.asc()).limit(limit)
        return list((await session.execute(stmt)).scalars())
    # Backward (default): newest-below-cursor first, then flip to ascending.
    if before:
        stmt = stmt.where(Message.id < before)
    stmt = stmt.order_by(Message.id.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars())
    rows.reverse()
    return rows
