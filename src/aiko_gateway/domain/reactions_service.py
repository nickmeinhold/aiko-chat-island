"""Emoji-reaction persistence + aggregation (#2634, v2 social layer).

REBUILT SIGNED + IDENTITY-BEARING (the reverted first cut was anonymous+unsigned).
A reaction carries the reactor's ``user_id`` (exposed on read) and its Ed25519
``origin`` envelope (the gateway CARRIES it, does not verify — identical posture to a
signed message). See ``docs/crucible/sovereign-reaction-signing/SIGNING-SPEC.md``.

The SINGLE mutator door for reactions (enforce-at-the-backend-through-one-door):
the REST routes, and any future in-process caller, add/remove through
``add_reaction`` / ``remove_reaction`` here — which VALIDATE the emoji themselves
(not trusting the route to have done it), so the closed-shape guarantee lives at the
door, not at one caller. The ``origin`` envelope is shape-validated UPSTREAM at the
trust boundary (``signing.validate_origin`` in the route, where the request's
``client_msg_id`` binding is in scope) and passed in already-validated to persist —
exactly the split messages use (``messages_service.create_outbound`` also takes a
pre-validated ``origin``). Reads are the viewer-dependent ``aggregate_for_messages``
projection folded into ``messages_service.message_view`` on the history path.

STATE, NOT EVENT — see the ``MessageReaction`` model docstring. There is no
forward-ULID reaction event and no separate feed: a reaction changes an aggregate
that ``message_view`` recomputes on every history read, so a missed live frame
self-heals the next time that message ROW is re-fetched (scroll-up / cold reload /
re-bind — NOT merely "on reconnect"). The live ``reaction`` WS frame
(``envelopes.reaction_frame``) is a best-effort latency optimisation over that
recomputed aggregate.

IDENTITY IS EXPOSED; BLOCK HIDES BOTH IDENTITY AND ITS CONTRIBUTION TO THE COUNT.
The v2 read API surfaces the reactor list, so the block predicate is applied to the
WHOLE projection: a reaction from a user in a block relationship with the viewer is
dropped from that viewer's ``reactors[]`` AND from that viewer's ``count`` (the count
is the length of the filtered reactor set). This is viewer-dependent CONSISTENTLY —
history aggregate, the mutate-response count, and who-receives the live frame all
apply the SAME block predicate, so there is no count oracle (the anonymous model's
hazard, where a viewer-dependent count disagreed with a global one and leaked that a
hidden user had reacted). The live ``reaction`` frame carries the identity delta with
NO server count; a subscriber in a block relationship with the reactor never receives
it, so their count never moves for someone they can't see. One predicate, all paths.

CONCURRENCY: ``add_reaction`` uses ``INSERT ... ON CONFLICT DO NOTHING`` (SQLite
dialect, matching dev+prod — CLAUDE.md), so a concurrent duplicate is a no-op AT THE
DB, not a check-then-insert race. A re-add of an emoji the user already placed keeps
the FIRST row's ``origin`` (the insert is ignored), exactly like a message resend
keeps the first row's origin — the idempotency key already pins the endorsement. The
Postgres path (deferred, #14) swaps to
``postgresql.insert(...).on_conflict_do_nothing()`` when that migration lands.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import MessageReaction

# Opaque-emoji length cap (defense-in-depth). A real emoji, incl. a ZWJ sequence
# (family/skin-tone/flag), is well under this; the cap just stops an unbounded blob
# masquerading as an emoji. Mirrors MessageReaction.emoji String(64).
MAX_EMOJI_LEN = 64

# Distinct emojis one user may place on one message. Bounds the single-actor payload/
# DoS vector: without it, one user × N distinct 64-char "emojis" = N rows, N aggregate
# buckets, N entries in every history reactions[] for that message. 20 mirrors the
# app's directory cap — comfortably above any real "how many reactions would a person
# add" while killing the amplifier.
MAX_REACTIONS_PER_USER_PER_MESSAGE = 20

# Max distinct emojis PROJECTED per message in a history page. The per-user cap bounds
# ONE actor; this bounds the MESSAGE-level amplifier (N colluding users × 20 each would
# still bloat one message's reactions[]). Enforced at the PROJECTION, not as a write
# cap: the top-N by count are returned (the rest are the long tail nobody renders), so
# there is no first-emoji-wins write race and every reaction still persists + counts
# toward its emoji if it's in the top band. 50 is far above any real message's
# distinct-emoji count while capping the wire array.
MAX_EMOJIS_PROJECTED_PER_MESSAGE = 50

# Max reactor identities PROJECTED per (message, emoji) in a history page. The COUNT
# stays the true (block-filtered) tally even when this truncates the list — the
# Slack/Telegram pattern (count is authoritative, faces are a bounded sample). Bounds
# the identity-payload weight on a hot emoji everyone piles onto. reacted_by_me is a
# separate flag, so the viewer's own participation is never lost to this truncation
# even if their identity falls past N (contrast the emoji-group truncation below, which
# MUST keep the viewer's own group or reacted_by_me for a rare emoji would vanish).
MAX_REACTORS_PROJECTED_PER_EMOJI = 20


class InvalidEmoji(ValueError):
    """The supplied emoji is empty/blank, over-long, or carries a structural hazard
    (a path separator or control char) — a controlled 422 at the route, never an
    FK/length error at commit or an un-deletable row."""


class ReactionLimitExceeded(Exception):
    """The user already holds ``MAX_REACTIONS_PER_USER_PER_MESSAGE`` distinct emojis
    on this message — a controlled 429 at the route, not an unbounded row spray.

    The cap is a SPRAY BOUND, not a hard quota: the check-then-insert is race-tolerant,
    so concurrent distinct adds can overshoot by a few. That is fine — it bounds a
    single-actor amplifier, it is NOT a correctness invariant, so do not later "enforce
    exactly N" with a serialized counter / DB constraint at this layer. The TEMPORAL
    fuse (rounds of add/remove toggling, concurrent-overshoot rate) is the per-IP
    ``rate_limit("reactions")`` on the routes, not this distinct-count cap."""


def validate_emoji(emoji: object) -> str:
    """Return ``emoji`` unchanged if it is a well-formed opaque reaction token, else
    raise ``InvalidEmoji``. The value is OPAQUE — never normalised or transformed (the
    app owns rendering; the exact UTF-8 bytes are what the client SIGNED, per the
    SIGNING-SPEC canonicalization rule) — but it MUST be safely representable
    everywhere it travels:

    * non-empty and within ``MAX_EMOJI_LEN`` (bounds storage);
    * equal to its own ``strip()`` (``"👍"`` and ``" 👍 "`` must not be distinct PKs /
      aggregate lines — reject the lookalikes rather than silently forking state);
    * no ``/`` and no ASCII control chars — defence-in-depth on the opaque token
      (neither belongs in a real emoji). DELETE addresses the emoji as a QUERY PARAM
      (percent-encoded end-to-end), so path-grammar chars like ``#`` ``?`` ``%`` are
      transport-safe and NOT rejected — keycap emoji (``#️⃣`` contains ``#``) round-trip.
      Lookalike multiplicity from invisible/format codepoints (ZWSP, bidi) is a KNOWN,
      ACCEPTED residual bounded by the per-user + per-message caps rather than closed.
    """
    if not isinstance(emoji, str):
        raise InvalidEmoji("emoji must be a string")
    if not emoji or emoji != emoji.strip():
        raise InvalidEmoji("emoji must be non-empty and surrounding-whitespace-free")
    if len(emoji) > MAX_EMOJI_LEN:
        raise InvalidEmoji(f"emoji exceeds {MAX_EMOJI_LEN} chars")
    if "/" in emoji or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in emoji):
        raise InvalidEmoji("emoji may not contain '/' or control characters")
    return emoji


async def emoji_count(
    session: AsyncSession, message_id: str, emoji: str, *,
    blocked_user_ids: set[str],
) -> int:
    """The VIEWER-DEPENDENT count of a single (message, emoji): reactors NOT in the
    viewer's block set. This is the number the mutate response carries — the same
    block-filtered tally the history aggregate computes, so a caller's POST/DELETE
    response is consistent with what a subsequent history read shows (no count oracle).
    The live ``reaction`` frame carries no count (it is an identity delta the client
    applies as a set change), so this is the ONLY count query."""
    stmt = select(func.count()).select_from(MessageReaction).where(
        MessageReaction.message_id == message_id,
        MessageReaction.emoji == emoji,
    )
    if blocked_user_ids:
        stmt = stmt.where(MessageReaction.user_id.notin_(blocked_user_ids))
    return (await session.execute(stmt)).scalar_one()


async def add_reaction(
    session: AsyncSession, *, user_id: str, message_id: str, emoji: str,
    origin: dict | None = None,
) -> bool:
    """Idempotently add ``user_id``'s ``emoji`` reaction to ``message_id`` carrying the
    pre-validated ``origin`` envelope (None for an unsigned reaction); return
    ``changed`` — True IFF a NEW row was actually inserted (so the caller fans out a
    live frame only on a real change, not a re-add no-op). The caller computes the
    viewer-dependent count via ``emoji_count`` after this returns.

    Validates the emoji HERE (the one door), raising ``InvalidEmoji``; enforces the
    per-(user,message) distinct-emoji cap, raising ``ReactionLimitExceeded``. Message
    existence + visibility remain the CALLER's responsibility (the route resolves the
    message through the channel ACL and rejects a soft-deleted / blocked-author target
    first). ``origin`` is shape-validated upstream (``signing.validate_origin``); it is
    persisted verbatim to be echoed on read — the gateway carries, does not verify.

    Idempotency is DB-enforced (``INSERT ... ON CONFLICT DO NOTHING``): a concurrent
    duplicate is a no-op at the database — not a check-then-insert race — and a re-add
    keeps the FIRST row's ``origin``, since the conflicting insert (with a fresh
    signature) is ignored. The idempotency key already pins the endorsement."""
    emoji = validate_emoji(emoji)
    # Cap only gates a NEW distinct emoji: re-adding an emoji the user already placed
    # is always allowed (idempotent no-op), so a user at the cap can still toggle their
    # existing reactions. A tiny over-cap race under concurrency is accepted (bounded).
    exists = await session.get(MessageReaction, (message_id, user_id, emoji))
    if exists is None:
        distinct = (await session.execute(
            select(func.count(func.distinct(MessageReaction.emoji))).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_id == user_id,
            ))).scalar_one()
        if distinct >= MAX_REACTIONS_PER_USER_PER_MESSAGE:
            raise ReactionLimitExceeded()
    result = await session.execute(
        sqlite_insert(MessageReaction)
        # created_at is pinned in .values() rather than left to the model's Python-side
        # `default=` — a Core ins().values() is NOT guaranteed to apply the ORM column
        # default the way session.add() does, and the column is NOT NULL with no
        # server_default. Explicit = engine-independent.
        .values(message_id=message_id, user_id=user_id, emoji=emoji, origin=origin,
                created_at=dt.datetime.now(dt.timezone.utc))
        .on_conflict_do_nothing())
    await session.commit()
    return result.rowcount > 0


async def remove_reaction(
    session: AsyncSession, *, user_id: str, message_id: str, emoji: str,
) -> bool:
    """Remove ``user_id``'s ``emoji`` reaction from ``message_id``; return ``changed``
    — True IFF a row was actually deleted (idempotent — removing an absent reaction
    deletes zero and is not an error, and the caller then skips the live frame). A
    conditional DELETE folded into one statement, no observe-then-write. Validates the
    emoji at the door.

    Removing is authorised by ROW OWNERSHIP (the caller deletes only their own
    ``user_id`` row), which is why it needs no ``origin``: un-reacting is a
    strictly-reducing self-owned action, allowed even after the caller loses sight of
    the message. A signed ``remove`` event (SIGNING-SPEC field ``action=remove``) is
    the client's to attest for a future reputation trail; the STATE model here records
    un-reaction as row absence, not a second signed event."""
    emoji = validate_emoji(emoji)
    result = await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == user_id,
            MessageReaction.emoji == emoji,
        ))
    await session.commit()
    return result.rowcount > 0


async def aggregate_for_messages(
    session: AsyncSession, message_ids: list[str], viewer_id: str, *,
    blocked_user_ids: set[str],
) -> dict[str, list[dict]]:
    """Reaction aggregate for a PAGE of messages, in ONE query — never N+1 per message.
    Returns ``{message_id: [{"emoji", "count", "reacted_by_me", "reactors": [...]},
    ...]}`` with an entry only for messages that have at least one (visible) reaction
    (absent == none, so the serializer defaults to ``[]``).

    IDENTITY-BEARING + BLOCK-FILTERED (bounded-A). Each emoji entry carries the reactor
    list — ``reactors`` is ``[{"user_id", "origin"?}, ...]`` (``origin`` present iff the
    reaction was signed) — bounded to ``MAX_REACTORS_PROJECTED_PER_EMOJI`` while
    ``count`` stays the FULL filtered tally (count authoritative, faces a bounded
    sample). Every viewer-dependent bit — ``count``, ``reactors``, ``reacted_by_me`` —
    is filtered by the SAME block predicate: a reaction from a user in
    ``blocked_user_ids`` (the viewer's block pairs, computed once by the caller) is
    dropped entirely. Since the live ``reaction`` frame is also block-filtered (fanout
    exclusion) and carries no count, all paths agree — no count oracle.

    Per-emoji rows are ordered ``(-count, emoji)`` — most-reacted first, ties broken by
    the opaque emoji string for a stable, deterministic wire order. Reactors within a
    group preserve insertion order (created_at, then user_id) for a stable sample."""
    if not message_ids:
        return {}
    rows = (await session.execute(
        select(
            MessageReaction.message_id, MessageReaction.emoji,
            MessageReaction.user_id, MessageReaction.origin,
        )
        .where(MessageReaction.message_id.in_(message_ids))
        .order_by(MessageReaction.created_at, MessageReaction.user_id))).all()

    # Group by (message_id, emoji), dropping blocked reactors (identity AND count).
    grouped: dict[tuple[str, str], list[dict]] = {}
    for message_id, emoji, user_id, origin in rows:
        if user_id in blocked_user_ids:
            continue
        reactor: dict = {"user_id": user_id}
        if origin is not None:
            reactor["origin"] = origin
        grouped.setdefault((message_id, emoji), []).append(reactor)

    by_msg: dict[str, list[dict]] = {}
    for (message_id, emoji), reactors in grouped.items():
        # count is the FULL filtered tally; reactors[] is a bounded sample of it. The
        # viewer's own participation rides reacted_by_me, so truncating the sample past
        # the viewer's own identity never loses their "you reacted" signal.
        by_msg.setdefault(message_id, []).append({
            "emoji": emoji,
            "count": len(reactors),
            "reacted_by_me": any(r["user_id"] == viewer_id for r in reactors),
            "reactors": reactors[:MAX_REACTORS_PROJECTED_PER_EMOJI],
        })

    for message_id, entries in by_msg.items():
        entries.sort(key=lambda e: (-e["count"], e["emoji"]))
        # Truncate the long tail so a multi-user emoji raid can't bloat one message's
        # wire array — top-N by count. BUT always keep the VIEWER'S OWN emoji groups
        # even past N: dropping an emoji the viewer reacted with would zero its
        # reacted_by_me on re-page (self-heal corruption for a rare emoji), unlike the
        # reactor-sample truncation above which reacted_by_me survives. Bound stays
        # finite: top-N ∪ mine ≤ N + per-user cap.
        if len(entries) > MAX_EMOJIS_PROJECTED_PER_MESSAGE:
            top = entries[:MAX_EMOJIS_PROJECTED_PER_MESSAGE]
            top_emojis = {e["emoji"] for e in top}
            mine_tail = [e for e in entries[MAX_EMOJIS_PROJECTED_PER_MESSAGE:]
                         if e["reacted_by_me"] and e["emoji"] not in top_emojis]
            by_msg[message_id] = top + mine_tail
    return by_msg


async def purge_user_reactions(session: AsyncSession, user_id: str) -> None:
    """Delete every reaction authored by ``user_id`` — the account-deletion cascade
    teardown for this FK-to-``users`` child (children-before-parent, no ON DELETE
    CASCADE; the cascade guard requires it). Caller owns the transaction/commit.

    NO ``reaction`` frames are emitted for the purged rows: account deletion is a
    COLD-RELOAD event, the state-not-event tradeoff at N-message blast radius. Live
    clients' counts on every message the deleted user touched self-heal on the next
    re-page of each. Named, not silent."""
    await session.execute(
        delete(MessageReaction).where(MessageReaction.user_id == user_id))


async def purge_reactions_for_messages(
    session: AsyncSession, message_ids: Select,
) -> None:
    """Delete every reaction on the messages selected by ``message_ids`` — the
    channel-hard-delete teardown (reactions FK ``messages.id``, so they must go
    before their messages, exactly like ``MessageReport`` in
    ``channels_service.hard_delete_channel``). Caller owns the transaction/commit."""
    await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id.in_(message_ids)))
