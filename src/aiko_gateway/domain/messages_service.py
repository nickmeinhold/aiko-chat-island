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
from . import channels_service, moderation_service, signing_keys_service
from .ids import new_ulid
from .models import Channel, Message, User


def message_view(m: Message) -> dict:
    """The stable MessageView the client contract exposes (plan §A1).

    This is the SINGLE serializer — REST history, WS ack-fanout, and bus-ingest
    fanout all pass through here, so echoing the signing `origin` here carries it
    on every read path at once. `origin` is included ONLY when present (signed
    gateway-side messages); it is omitted for unsigned + bus-born rows so an
    absent key reads as "unverified", per the app's verifier contract (#1816)."""
    view = {
        "msg_id": m.id,
        "channel_id": m.channel_id,
        "sender": {"user_id": m.sender_user_id, "kind": m.sender_kind, "label": m.sender_label},
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        "reply_to": m.reply_to,
    }
    if m.origin is not None:
        view["origin"] = m.origin
    return view


async def create_outbound(
    session: AsyncSession, *, user: User, channel: Channel,
    body: str, client_msg_id: str, reply_to: str | None = None,
    origin: dict | None = None,
) -> tuple[Message, bool]:
    """Persist a user's outgoing message (server ULID, server-derived sender —
    invariant I5). Idempotent on (channel, client_msg_id): a resend returns the
    existing row. Returns (row, created).

    `origin` is the SHAPE-validated sovereign-signing envelope (already checked by
    domain/signing.validate_origin at the call site, incl. that its client_msg_id
    equals this one). It is carried verbatim; the gateway does not verify it. A
    resend keeps the FIRST row's origin — the idempotency key already pins the
    stored message, so a differing re-signed envelope on a retry is ignored, not a
    second row (consistent with the existing client_msg_id no-op contract).

    When `origin` is present, the sender's pubkey->account binding is observed at
    send time through the single door `signing_keys_service.record_signing_key`
    (#1816 PR B) — the IMPLICIT half of key binding. It is recorded BEFORE the
    Message is added so the idempotency SAVEPOINT inside `record_signing_key` wraps
    only the key row (never the un-flushed Message), and the key + message land in
    this function's ONE commit — atomic, so a signed message can never persist
    without its binding. A resend short-circuits above and does not re-record (the
    binding already exists from the first send)."""
    existing = (await session.execute(
        select(Message).where(
            Message.channel_id == channel.id,
            Message.client_msg_id == client_msg_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if origin is not None:
        await signing_keys_service.record_signing_key(
            session, user_id=user.id,
            pubkey=origin["sender_pubkey"], key_version=origin["key_version"])
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
        origin=origin,
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
    the bus is a subset of HyperSpace's canonical set.

    Why that subset claim holds (drift-vector check, #6, verified post-#8): the
    ONLY runtime caller is main._ingest, reached via the actor's `_on_payload`,
    which fires ONLY for SUBSCRIBED topics. Since #8 subscriptions are gated to
    {bootstrap "general"} ∪ the `channel_list` EC share, every channel that can
    reach here is canonical — the upsert can never MINT a non-HyperSpace channel.
    The gate is structural (you cannot receive a message for an unsubscribed
    topic), not a prose check. On removal the actor unsubscribes BEFORE the DB
    delete, so no message can re-mint a just-removed channel. Residual: the
    hardcoded "general" bootstrap floor is not itself channel_list-gated — a
    negligible risk, as "general" is permanent; a DB-layer guard would re-couple
    the asyncio side to the aiko-thread channel_list cache (against #7) for no
    real gain. Single creation path:
    `channels_service.upsert_channel` (which flushes, not commits), so the
    channel upsert + message insert land in this function's ONE final commit —
    atomic, no orphan-channel-on-message-failure (cage-match PR#12, Carnot P1b)."""
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


async def get_message(session: AsyncSession, message_id: str) -> Message | None:
    """Fetch a single message row by id, or None. Used by the send path's
    reply-to interaction gate (#7) to resolve the author of the replied-to
    message. Does NOT filter on visibility — the caller decides what to do with
    a soft-deleted or otherwise-hidden target."""
    return await session.get(Message, message_id)


async def latest_ulid(session: AsyncSession, channel_id: str, viewer_id: str) -> str:
    """The newest *visible* message id in a channel FOR `viewer_id` — the
    live/history *fence* a `suback` carries (design 04 §Gap 2). Returns ``""`` for
    a channel with no visible messages: an empty fence means "no history boundary,
    everything is forward/live".

    The visibility filter MUST match ``get_history`` exactly: the fence and the
    history pager are two reads of the same id axis, and B4's reconnect loop pages
    history "until cursor >= fence", treating an empty page while ``cursor < fence``
    as an invariant violation (design 04 round 5). If the fence could point past
    the newest visible row, that termination condition would be unreachable by
    visible rows and the violation check would false-positive. One predicate, both
    reads — the partition stays clean and the invariant stays assertable.

    Visibility has TWO dimensions, both viewer-INdependent EXCEPT blocks: a
    soft-deleted row (``deleted_at IS NULL``) is hidden from everyone, while a
    BLOCKED author's row is hidden only from the viewer in the block relationship
    (#7). That is why the fence is now per-viewer: blocker and non-blocker can see
    a different newest-visible message in the same channel.

    COUPLING IS WITHIN-INSTANT, NOT TIME-REVERSIBLE (cage-match Carnot HIGH). The
    fence and the history pager share this predicate, so at any single DB instant
    they agree. They do NOT agree across a *visibility shrink between* the fence
    read (at subscribe) and the client's later history paging: if a message that
    was visible at fence-time becomes hidden before paging — a new block here, OR a
    soft-delete (this race PRE-DATES blocks; #7 only widens its likelihood) — the
    already-issued fence can point at a row history now refuses to return, and B4's
    pager hits its empty-page-before-fence guard. In RELEASE this self-heals: the
    next reconnect's subscribe recomputes the fence with the now-current visibility,
    so blocker and history agree and the loop converges. The durable fix is making
    B4 treat empty-page-before-fence as a benign re-sync (refetch the fence) rather
    than an assert — a CLIENT/protocol change tracked in the app repo, not here.
    """
    result = await session.execute(
        select(func.max(Message.id)).where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
            moderation_service.not_blocked_predicate(viewer_id),
        )
    )
    return result.scalar_one() or ""


async def get_history(
    session: AsyncSession,
    channel_id: str,
    viewer_id: str,
    *,
    before: str | None = None,
    after: str | None = None,
    limit: int,
) -> list[Message]:
    """A page of messages in a channel visible to `viewer_id`, **always returned
    ascending** (oldest first) for display. Two cursor directions, mutually
    exclusive — a ULID is a total order, so both walk the same axis:

    * ``before`` (backward, the default — UI scroll-up): the ``limit`` newest
      messages with ``id < before``. Used to load older history a page at a time.
    * ``after`` (forward — B4 reconnect catch-up): the ``limit`` oldest messages
      with ``id > after``. Forward paging fills the oldest gap first, which is
      what makes ``MAX(serverUlid)`` a crash-resumable watermark on the client
      (design 04 §Gap 2). ``after`` wins if both are passed.

    Visibility filter: soft-deleted rows are hidden from all; a blocked author's
    rows are hidden from the viewer in the block relationship (#7). This MUST be
    the same predicate ``latest_ulid`` (the fence) uses — see its docstring.
    """
    stmt = select(Message).where(
        Message.channel_id == channel_id,
        Message.deleted_at.is_(None),
        moderation_service.not_blocked_predicate(viewer_id),
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
