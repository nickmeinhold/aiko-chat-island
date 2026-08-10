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
from . import acl, channels_service, dm_service, moderation_service, signing_keys_service
from .ids import new_ulid
from .models import Channel, ChannelKind, Message, Retraction, User


class BlockedDmSend(Exception):
    """A send to a DM channel where the sender is in a block relationship with the DM
    peer (#2633, design 11 §Decision 5). Raised from the SINGLE mutator door
    (``create_outbound``) so EVERY send path — the WS route today, any REST/bot/in-process
    writer tomorrow — enforces the refuse-under-block rule, not just the transport adapter
    (cage-match PR#124 Tesla: seal the mutator, not the route). The WS route maps this to
    the existence-hiding ``no_channel`` error."""


def message_view(m: Message, *, reactions: list[dict] | None = None) -> dict:
    """The stable MessageView the client contract exposes (plan §A1).

    This is the SINGLE serializer — REST history, WS ack-fanout, and bus-ingest
    fanout all pass through here, so echoing the signing `origin` here carries it
    on every read path at once. `origin` is included ONLY when present (signed
    gateway-side messages); it is omitted for unsigned + bus-born rows so an
    absent key reads as "unverified", per the app's verifier contract (#1816).

    `reactions` (#2634) is the viewer-dependent, block-filtered aggregate
    [{emoji, count, reacted_by_me, reactors: [{user_id, origin?}]}] for this message,
    computed ONCE per page by reactions_service.aggregate_for_messages and injected by
    the history route. It defaults to `[]` — so the WS ack/message-fanout and
    bus-ingest paths (a FRESH message, which by construction has no reactions yet)
    serialize with an empty list and never need to touch the reactions table. A
    reaction that lands LATER rides its own discrete `reaction` frame
    (envelopes.reaction_frame), not a re-serialised message; the aggregate here is what
    a subsequent history read recomputes (state-not-event — see MessageReaction)."""
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
    # Key-bound @-mention spans (#2632), carried verbatim. Included ONLY when the
    # message actually has mentions — an absent key reads as "no mentions", the
    # same omit-when-empty contract as `origin`. Truthiness (not `is not None`) is
    # defense AT READ: create_outbound normalizes empty→NULL at write, but a truthy
    # check ALSO omits a stray stored `[]` (from a direct ORM insert / future
    # writer), so `mentions: []` can never reach the wire regardless of which door
    # wrote the row — one representation of "no mentions" as an invariant, not a
    # single-writer convention. The client re-resolves each span's id->current-handle
    # at render (targets key off the opaque identity, so a rename never orphans the
    # mention — app tab ADR-0004).
    if m.mentions:
        view["mentions"] = m.mentions
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


def should_federate(channel: Channel) -> bool:
    """Whether a message on ``channel`` may be PUBLISHED to the shared aiko bus. FALSE for
    a DM — dual-gated on BOTH ``kind != 'dm'`` AND the ``dm:`` prefix (#2633, design 11
    §Decision 3), so a lone kind retint can't re-federate a private room. This lives in the
    DOMAIN (not the ws route) so EVERY send path shares the one privacy decision — the same
    mutator-door law Decision 5's block gate follows (cage-match PR#124 round 12 Tesla P1:
    federation-egress and block-refuse are one trust class; both must live behind one door,
    not one sealed at the mutator and one at the transport)."""
    return channel.kind != ChannelKind.DM and not dm_service.is_dm_channel_name(
        channel.aiko_channel)


async def create_outbound(
    session: AsyncSession, *, user: User, channel: Channel,
    body: str, client_msg_id: str, reply_to: str | None = None,
    origin: dict | None = None, mentions: list[dict] | None = None,
) -> tuple[Message, bool]:
    """Persist a user's outgoing message (server ULID, server-derived sender —
    invariant I5). Idempotent on (channel, client_msg_id): a resend returns the
    existing row. Returns (row, created). The caller decides bus federation via the
    shared domain predicate ``should_federate(channel)`` (never its own) — cage-match
    PR#124 round 12 Tesla P1: the DM egress decision lives in ONE domain function so a
    second send path can't forget it.

    `origin` is the SHAPE-validated sovereign-signing envelope (already checked by
    domain/signing.validate_origin at the call site, incl. that its client_msg_id
    equals this one). It is carried verbatim; the gateway does not verify it. A
    resend keeps the FIRST row's origin — the idempotency key already pins the
    stored message, so a differing re-signed envelope on a retry is ignored, not a
    second row (consistent with the existing client_msg_id no-op contract).

    `mentions` is the SHAPE+caps-validated list of key-bound @-mention spans
    (already checked by domain/mentions.validate_mentions at the call site).
    Carried verbatim, resolver-free, and — like `origin` — a resend keeps the
    FIRST row's spans (the early-return above never reaches this insert on a
    resend). See the Message.mentions model note for the carrier contract.

    When `origin` is present, the sender's pubkey->account binding is observed at
    send time through the single door `signing_keys_service.record_signing_key`
    (#1816 PR B) — the IMPLICIT half of key binding. It is recorded BEFORE the
    Message is added so the idempotency SAVEPOINT inside `record_signing_key` wraps
    only the key row (never the un-flushed Message), and the key + message land in
    this function's ONE commit — atomic, so a signed message can never persist
    without its binding. A resend short-circuits above and does not re-record (the
    binding already exists from the first send)."""
    # IDEMPOTENCY FIRST (cage-match PR#124 Carnot F2): a resend of an already-persisted
    # (channel, client_msg_id) returns the existing row — even if a block was established
    # AFTER the original send. The block must stop NEW residue, not break reconciliation of
    # a message legitimately sent before the block (returning the existing row leaks no new
    # content — it is already block-filtered from the peer's reads).
    existing = (await session.execute(
        select(Message).where(
            Message.channel_id == channel.id,
            Message.client_msg_id == client_msg_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing, False
    # DM SEND UNDER A BLOCK → REFUSE, at the MUTATOR door so every send path enforces it
    # (#2633, design 11 §Decision 5; cage-match PR#124 Tesla P1a) — but only for a NEW
    # write (after the idempotency return above). The peer is resolved from the CANONICAL
    # PAIR (the dm: key), NOT live membership (cage-match PR#124 round 9 Tesla/Carnot P0):
    # a peer who BLOCKED then LEFT would vanish from the membership census, silently
    # disabling the block and letting the remaining member accumulate residue the leaver
    # sees on re-open. The pair is immutable, so it is the stable identity to block against.
    # DM-only; public/community block stays a read-only content filter.
    if channel.kind == ChannelKind.DM:
        peer_ids = dm_service.canonical_peer_ids(channel.aiko_channel, user.id)
        if peer_ids is None:
            # Malformed dm: key (anomalous kind='dm' row) — can't resolve the peer to
            # check the block, so FAIL CLOSED rather than skip Decision 5 (cage-match
            # PR#124 round 10 Carnot). A well-formed DM never hits this.
            raise BlockedDmSend()
        if peer_ids:
            blocked = await moderation_service.blocked_pair_user_ids(session, user.id)
            if any(p in blocked for p in peer_ids):
                raise BlockedDmSend()
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
        # Normalize empty→None so storage holds ONE representation of "no mentions".
        # This is the WRITE half of the defense; message_view's echo uses TRUTHINESS
        # (`if m.mentions:`), so even a stray stored `[]` from another writer is
        # omitted at READ — the two together make "no mentions omits" an invariant,
        # not a single-writer convention. (Do NOT switch the view to `is not None`:
        # that would re-admit a stored `[]` onto the wire.)
        mentions=mentions or None,
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
    try:
        channel = await channels_service.upsert_channel(session, msg.channel)
    except channels_service.ReservedDmChannel:
        # A bus message named a reserved dm: channel (anomalous — a DM never rides the
        # bus). DROP it: never persist bus traffic into a private DM (#2633, cage-match
        # PR#124 Tesla). Returning None mirrors the no-channel drop above.
        return None

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


async def visible_message(
    session: AsyncSession, viewer_id: str, message_id: str
) -> Message | None:
    """The message IFF ``viewer_id`` may currently SEE it — exists, not soft-deleted
    (taken-down), its channel is readable by the viewer (ACL), and its author is not in
    a block relationship with the viewer — else None. This is the SINGLE message-
    visibility predicate, mirroring ``get_history``'s filters; ``GET /v1/messages/{id}``
    (reply-parent resolution, #2633) and the reaction routes both delegate here so a
    "can I see this message?" answer can never drift between call sites. Callers choose
    whether a None is a 404 (existence-hiding) or a silent suppression.

    Deliberately does NOT resurrect a taken-down parent's body — a soft-deleted message
    is None (a 404 for the fetch-by-id path), never its content, so a reply preview can
    never leak retracted text (the retraction-leak the #2633 contract warns about)."""
    msg = await get_message(session, message_id)
    if msg is None or msg.deleted_at is not None:
        return None
    if await acl.readable_channel(session, viewer_id, msg.channel_id) is None:
        return None
    if msg.sender_user_id is not None:
        blocked = await moderation_service.blocked_pair_user_ids(session, viewer_id)
        if msg.sender_user_id in blocked:
            return None
    return msg


async def last_visible_message(
    session: AsyncSession, channel_id: str, viewer_id: str
) -> Message | None:
    """The newest message in ``channel_id`` VISIBLE to ``viewer_id`` (not soft-deleted,
    author not blocked), or None. Uses the SAME message-visibility filters as
    ``get_history`` / ``latest_ulid`` (block + soft-delete), so a preview never shows a
    line the channel read would hide. Retractions are not messages, so they never
    surface as a ``last_message`` (a channel whose only newer event is a takedown shows
    the last still-visible line).

    ACL PRECONDITION (same contract as ``get_history``): this does NOT check whether the
    viewer may READ ``channel_id`` — it applies only the block + soft-delete content
    filters. The caller MUST have already authorized the channel (``list_dms`` scopes to
    the viewer's OWN membership; a route serving a client-supplied channel_id must gate
    with ``acl.readable_channel`` first, exactly as the history route does). Named to
    stop a future caller from mistaking the "visible" in the name for an access check —
    it is a content filter, not a trust boundary."""
    return (await session.execute(
        select(Message)
        .where(
            Message.channel_id == channel_id,
            Message.deleted_at.is_(None),
            moderation_service.not_blocked_predicate(viewer_id),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )).scalar_one_or_none()


async def last_visible_messages(
    session: AsyncSession, channel_ids: list[str], viewer_id: str
) -> dict[str, Message]:
    """Batched ``last_visible_message`` for many channels — ``{channel_id: newest visible
    Message}`` (channels with no visible message are absent). Collapses ``GET /v1/dm``'s
    per-channel N+1 (cage-match PR#124: Kelvin/Carnot/Tesla) into TWO queries: the max
    visible id per channel, then those message rows. Applies the IDENTICAL block +
    soft-delete predicate as the singular ``last_visible_message`` (so the batched and
    single paths agree), and carries the SAME ACL PRECONDITION — the caller must have
    already authorized every id in ``channel_ids`` (``list_dms`` passes only the viewer's
    own DM channels)."""
    if not channel_ids:
        return {}
    # Newest visible id per channel (ULIDs are monotonic, so MAX(id) == newest).
    max_ids = [mid for (mid,) in (await session.execute(
        select(func.max(Message.id))
        .where(
            Message.channel_id.in_(channel_ids),
            Message.deleted_at.is_(None),
            moderation_service.not_blocked_predicate(viewer_id),
        )
        .group_by(Message.channel_id)
    )).all() if mid is not None]
    if not max_ids:
        return {}
    rows = (await session.execute(
        select(Message).where(Message.id.in_(max_ids))
    )).scalars()
    return {m.channel_id: m for m in rows}


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
