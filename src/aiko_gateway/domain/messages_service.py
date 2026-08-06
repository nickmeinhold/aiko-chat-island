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
from .models import Channel, Message, Retraction, User


def message_view(m: Message, *, reactions: list[dict] | None = None) -> dict:
    """The stable MessageView the client contract exposes (plan §A1).

    This is the SINGLE serializer — REST history, WS ack-fanout, and bus-ingest
    fanout all pass through here, so echoing the signing `origin` here carries it
    on every read path at once. `origin` is included ONLY when present (signed
    gateway-side messages); it is omitted for unsigned + bus-born rows so an
    absent key reads as "unverified", per the app's verifier contract (#1816).

    `reactions` (#2634) is the viewer-dependent aggregate [{emoji, count,
    reacted_by_me}] for this message, computed ONCE per page by
    reactions_service.aggregate_for_messages and injected by the history route.
    It defaults to `[]` — so the WS ack/message-fanout and bus-ingest paths (a
    FRESH message, which by construction has no reactions yet) serialize with an
    empty list and never need to touch the reactions table. A reaction that lands
    LATER rides its own discrete `reaction` frame (envelopes.reaction_frame), not a
    re-serialised message; the aggregate here is what a subsequent history read
    recomputes (state-not-event — see the MessageReaction model)."""
    view = {
        "msg_id": m.id,
        "channel_id": m.channel_id,
        "sender": {"user_id": m.sender_user_id, "kind": m.sender_kind, "label": m.sender_label},
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        "reply_to": m.reply_to,
        "reactions": reactions or [],
    }
    if m.origin is not None:
        view["origin"] = m.origin
    return view


def retraction_view(r: Retraction) -> dict:
    """Wire item for a takedown retraction — used BOTH as a heterogeneous item in
    the `get_history` stream and as the WS `retraction` frame body (envelopes.
    retraction_frame). `id` advances the client's forward watermark exactly like a
    message id (that is what makes the deletion catch-uppable); `target_msg_id` is
    the message the client must suppress + remove. `id > target_msg_id` holds by
    construction — see the Retraction model. `type` disambiguates it from a
    `message` item in the interleaved history array (wire contract, #7)."""
    return {
        "type": "retraction",
        "id": r.id,
        "target_msg_id": r.target_msg_id,
        "channel_id": r.channel_id,
    }


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

    The axis now carries takedown ``Retraction`` events too (#7), so the fence is
    the newest of (visible messages) ∪ (channel retractions) — exactly the set
    ``get_history`` can return. Were the fence to ignore retractions, a retraction
    with an id above the newest visible message would land in the "id > fence → live"
    partition on a fresh subscribe and never be replayed. Retractions are NOT
    block-filtered (they carry no content, only remove — see ``get_history``'s
    add/remove asymmetry note), so the retraction component here matches ``get_history``
    by ALSO not filtering them; the paired read stays consistent because both reads
    apply the identical (channel-only) retraction predicate.

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

    Retractions do NOT add to this shrink race: because they are never block-filtered
    (#7 add/remove asymmetry), no block/unblock transition can hide a retraction that
    was in a viewer's fence, so a takedown always propagates on forward catch-up
    regardless of block state. Only the message component carries the visibility-shrink
    coupling described above.
    """
    msg_result = await session.execute(
        select(func.max(Message.id)).where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
            moderation_service.not_blocked_predicate(viewer_id),
        )
    )
    ret_result = await session.execute(
        select(func.max(Retraction.id)).where(Retraction.channel_id == channel_id)
    )
    # "" is lexicographically below any 26-char ULID, so max() picks the newer real
    # id and collapses to "" only when the channel has neither a visible message nor
    # a retraction.
    return max(msg_result.scalar_one() or "", ret_result.scalar_one() or "")


async def get_history(
    session: AsyncSession,
    channel_id: str,
    viewer_id: str,
    *,
    before: str | None = None,
    after: str | None = None,
    limit: int,
) -> list[Message | Retraction]:
    """A page of channel history visible to `viewer_id`, **always returned
    ascending** (oldest first) for display. The stream is HETEROGENEOUS: `Message`
    rows AND `Retraction` events (takedown propagation, #7), interleaved on the one
    shared ULID axis — both types carry an `id` on the same monotonic order, so a
    single cursor pages them together. Two cursor directions, mutually exclusive:

    * ``before`` (backward, the default — UI scroll-up): the ``limit`` newest items
      with ``id < before``. Used to load older history a page at a time.
    * ``after`` (forward — B4 reconnect catch-up): the ``limit`` oldest items with
      ``id > after``. Forward paging fills the oldest gap first, which is what makes
      ``MAX(serverUlid)`` a crash-resumable watermark on the client (design 04
      §Gap 2). ``after`` wins if both are passed. A retraction with
      ``id > client watermark`` is delivered here — that is how an offline client
      catches up on a deletion it never re-observes otherwise.

    Visibility — the ADD/REMOVE asymmetry (#7). Messages carry CONTENT, so they are
    filtered: soft-deleted rows are hidden from all; a blocked author's rows are
    hidden from the viewer in the block relationship. This MUST be the same predicate
    ``latest_ulid`` (the fence) uses — see its docstring. Retractions are the opposite
    kind of event: they carry NO content and only ever REMOVE something, so they are
    NOT block-filtered — they ride the unfiltered stream to every channel member, the
    way Matrix redactions / Discord deletes reach everyone in the room while block
    stays a content filter. A delete can only reduce what you see, so it needs no
    permission to be delivered; and block-filtering it would only STRAND takedowns
    across a block/unblock epoch (a monotonic watermark can't reach back to replay a
    delete it skipped) for zero privacy benefit — the id is opaque and unattributable.
    (Retractions are channel-scoped only; no ``deleted_at`` filter — the target's
    soft-delete is exactly what they announce.)

    Interleave/limit correctness: each type is queried for its own ``limit`` items
    above/below the cursor, the two ascending (or descending) streams are merged by
    ``id`` and truncated to ``limit``. Because each sub-stream already yielded its
    ``limit`` smallest (or largest) qualifying ids, no item that belongs inside the
    page can hide beyond position ``limit`` of the union.
    """
    msg_stmt = select(Message).where(
        Message.channel_id == channel_id,
        Message.deleted_at.is_(None),
        moderation_service.not_blocked_predicate(viewer_id),
    )
    ret_stmt = select(Retraction).where(Retraction.channel_id == channel_id)
    if after is not None:
        # Forward: oldest-above-cursor first from each stream, merge ascending.
        msg_stmt = msg_stmt.where(Message.id > after).order_by(Message.id.asc()).limit(limit)
        ret_stmt = ret_stmt.where(Retraction.id > after).order_by(Retraction.id.asc()).limit(limit)
        msgs = list((await session.execute(msg_stmt)).scalars())
        rets = list((await session.execute(ret_stmt)).scalars())
        return sorted([*msgs, *rets], key=lambda r: r.id)[:limit]
    # Backward (default): newest-below-cursor first from each stream, merge
    # descending, truncate, then flip to ascending for display.
    if before:
        msg_stmt = msg_stmt.where(Message.id < before)
        ret_stmt = ret_stmt.where(Retraction.id < before)
    msg_stmt = msg_stmt.order_by(Message.id.desc()).limit(limit)
    ret_stmt = ret_stmt.order_by(Retraction.id.desc()).limit(limit)
    msgs = list((await session.execute(msg_stmt)).scalars())
    rets = list((await session.execute(ret_stmt)).scalars())
    rows = sorted([*msgs, *rets], key=lambda r: r.id, reverse=True)[:limit]
    rows.reverse()
    return rows
