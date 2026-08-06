"""Emoji-reaction persistence + aggregation (#2634, v2 social layer).

The SINGLE mutator door for reactions (enforce-at-the-backend-through-one-door):
the REST routes, and any future in-process caller, add/remove through
``add_reaction`` / ``remove_reaction`` here — which VALIDATE the emoji themselves
(not trusting the route to have done it), so the closed-shape guarantee lives at
the door, not at one caller (cage-match Tesla: "validation belongs inside add/
remove, or the one door is theater"). Reads are the viewer-dependent
``aggregate_for_messages`` projection folded into ``messages_service.message_view``
on the history path.

STATE, NOT EVENT — see the ``MessageReaction`` model docstring. There is no
forward-ULID reaction event and no separate feed: a reaction changes an aggregate
that ``message_view`` recomputes on every history read, so a missed live frame
self-heals the next time that message ROW is re-fetched (scroll-up / cold reload /
re-bind — NOT merely "on reconnect": a client that keeps synced messages resident
and only forward-pages never re-reads the row, so its aggregate stays frozen until
it re-fetches). The live ``reaction`` WS frame (``envelopes.reaction_frame``) is a
best-effort latency optimisation over that recomputed aggregate.

COUNTS ARE ANONYMOUS GLOBAL AGGREGATES; BLOCK HIDES IDENTITY, NOT THE TALLY
(cage-match Carnot/Tesla round 2). The v2 read API exposes NO reactor list — a
reaction is an anonymous ``count`` + the viewer's own ``reacted_by_me``, never a
"who reacted". So the COUNT is the same global number on every path — history
aggregate, the mutate response, and the WS ``reaction`` frame — and is NOT
block-filtered. Filtering it (an earlier attempt) created a *count oracle*: a
viewer-dependent count that disagreed with the global frame count would reveal that
a blocked user had reacted (only-a-blocked-user-holds-👍, a visible peer adds 👍,
viewer's history says 1 but the frame says 2 → "someone I can't see reacted"). A
blocked user shows as messages / identity, so what must respect the block is the
IDENTITY-bearing live frame — it carries the reactor's ``user_id`` — which the
route's fanout exclusion drops for the reactor's ∪ message author's block pairs.
The anonymous count rides through unfiltered, exactly like Slack/Discord show a
global reaction count regardless of who you've blocked. One number, all paths; the
only viewer-dependent bits are ``reacted_by_me`` (self, always visible) and WHO
receives the identity-bearing frame.

CONCURRENCY: ``add_reaction`` uses ``INSERT ... ON CONFLICT DO NOTHING`` (SQLite
dialect, matching dev+prod — CLAUDE.md), so a concurrent duplicate is a no-op AT THE
DB, not a check-then-insert race (the class Kelvin/Carnot/Tesla flagged). The Postgres
path (deferred, #14) swaps to ``postgresql.insert(...).on_conflict_do_nothing()`` —
one line, same semantics — when that migration lands.
"""
from __future__ import annotations

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import MessageReaction

# Opaque-emoji length cap (defense-in-depth). A real emoji, incl. a ZWJ sequence
# (family/skin-tone/flag), is well under this; the cap just stops an unbounded blob
# masquerading as an emoji. Mirrors MessageReaction.emoji String(64).
MAX_EMOJI_LEN = 64

# Distinct emojis one user may place on one message. Bounds the single-actor payload/
# DoS vector (cage-match Tesla): without it, one user × N distinct 64-char "emojis" =
# N rows, N aggregate buckets, N entries in every history reactions[] for that message.
# 20 mirrors the app's directory cap — comfortably above any real "how many reactions
# would a person add" while killing the amplifier.
MAX_REACTIONS_PER_USER_PER_MESSAGE = 20

# Max distinct emojis PROJECTED per message in a history page. The per-user cap bounds
# ONE actor; this bounds the MESSAGE-level amplifier (cage-match round 5, Tesla — N
# colluding users × 20 each would still bloat one message's reactions[]). Enforced at
# the PROJECTION, not as a write cap: the top-N by count are returned (the rest are the
# long tail nobody renders), so there is no first-emoji-wins write race and every
# reaction still persists + counts toward its emoji if it's in the top band. 50 is far
# above any real message's distinct-emoji count while capping the wire array.
MAX_EMOJIS_PROJECTED_PER_MESSAGE = 50


class InvalidEmoji(ValueError):
    """The supplied emoji is empty/blank, over-long, or carries a structural hazard
    (a path separator or control char) — a controlled 422 at the route, never an
    FK/length error at commit or an un-deletable row."""


class ReactionLimitExceeded(Exception):
    """The user already holds ``MAX_REACTIONS_PER_USER_PER_MESSAGE`` distinct emojis
    on this message — a controlled 429 at the route, not an unbounded row spray.

    The cap is a SPRAY BOUND, not a hard quota (cage-match Tesla): the check-then-insert
    is race-tolerant, so concurrent distinct adds can overshoot by a few. That is fine —
    it bounds a single-actor amplifier, it is NOT a correctness invariant, so do not
    later "enforce exactly N" with a serialized counter / DB constraint at this layer.
    The TEMPORAL fuse (rounds of add/remove toggling, concurrent-overshoot rate) is the
    per-IP ``rate_limit("reactions")`` on the routes, not this distinct-count cap — the
    two bound different axes (breadth vs rate; cage-match round 4 Carnot/Tesla)."""


def validate_emoji(emoji: object) -> str:
    """Return ``emoji`` unchanged if it is a well-formed opaque reaction token, else
    raise ``InvalidEmoji``. The value is OPAQUE — never normalised or transformed (the
    app owns rendering) — but it MUST be safely representable everywhere it travels:

    * non-empty and within ``MAX_EMOJI_LEN`` (bounds storage);
    * equal to its own ``strip()`` (cage-match Tesla: ``"👍"`` and ``" 👍 "`` must not
      be three distinct PKs / aggregate lines — reject the lookalikes rather than
      silently forking state);
    * no ``/`` and no ASCII control chars (cage-match Carnot: DELETE addresses the
      emoji as a single URL path segment, so a stored ``"a/b"`` would be un-removable
      via ``/reactions/{emoji}`` — a row you can create but not delete).
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


async def _count(session: AsyncSession, message_id: str, emoji: str) -> int:
    """Live count of a single (message, emoji) — the number the mutator returns and
    the ``reaction`` frame carries, so the client reconciles to the server's truth
    rather than trusting its optimistic increment."""
    return (await session.execute(
        select(func.count()).select_from(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.emoji == emoji,
        ))).scalar_one()


async def add_reaction(
    session: AsyncSession, *, user_id: str, message_id: str, emoji: str,
) -> tuple[int, bool]:
    """Idempotently add ``user_id``'s ``emoji`` reaction to ``message_id``; return
    ``(count, changed)`` where ``count`` is the resulting live count for that emoji and
    ``changed`` is True IFF a new row was actually inserted (so the caller fans out a
    live frame only on a real change, not on a re-add no-op — cage-match Tesla).

    Validates the emoji HERE (the one door), raising ``InvalidEmoji``; enforces the
    per-(user,message) distinct-emoji cap, raising ``ReactionLimitExceeded``. Message
    existence + visibility remain the CALLER's responsibility (the route resolves the
    message through the channel ACL and rejects a soft-deleted / blocked-author target
    first), mirroring ``moderation_service.report_message``.

    Idempotency is DB-enforced (``INSERT ... ON CONFLICT DO NOTHING``), so a concurrent
    duplicate is a no-op at the database — not a check-then-insert race."""
    emoji = validate_emoji(emoji)
    # Cap only gates a NEW distinct emoji: re-adding an emoji the user already placed
    # is always allowed (idempotent no-op), so a user at the cap can still toggle their
    # existing reactions. A tiny over-cap race under concurrency is accepted (bounded,
    # same MVP tolerance as the rest of the layer) — it caps a spray, not a correctness
    # invariant.
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
        .values(message_id=message_id, user_id=user_id, emoji=emoji)
        .on_conflict_do_nothing())
    await session.commit()
    changed = result.rowcount > 0
    return await _count(session, message_id, emoji), changed


async def remove_reaction(
    session: AsyncSession, *, user_id: str, message_id: str, emoji: str,
) -> tuple[int, bool]:
    """Remove ``user_id``'s ``emoji`` reaction from ``message_id``; return
    ``(count, changed)`` where ``changed`` is True IFF a row was actually deleted
    (idempotent — removing an absent reaction deletes zero and is not an error, and
    the caller then skips the live frame). A conditional DELETE folded into one
    statement, no observe-then-write. Validates the emoji at the door."""
    emoji = validate_emoji(emoji)
    result = await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == user_id,
            MessageReaction.emoji == emoji,
        ))
    await session.commit()
    return await _count(session, message_id, emoji), result.rowcount > 0


async def aggregate_for_messages(
    session: AsyncSession, message_ids: list[str], viewer_id: str,
) -> dict[str, list[dict]]:
    """Reaction aggregate for a PAGE of messages, in ONE grouped query (+ one for the
    viewer's own rows) — never N+1 per message. Returns ``{message_id: [{"emoji",
    "count", "reacted_by_me"}, ...]}`` with an entry only for messages that have at
    least one reaction (absent == none, so the serializer defaults to ``[]``).

    ``count`` is the GLOBAL anonymous tally — NOT block-filtered (cage-match round 2).
    The v2 API exposes no reactor list, so a count reveals no identity; block-filtering
    it only created a *count oracle* (a viewer-dependent count disagreeing with the
    global WS-frame count would leak that a blocked user reacted). The identity a block
    must hide rides the live ``reaction`` frame (its ``user_id``), which the route's
    fanout exclusion drops — the count itself is global on every path (history, mutate
    response, frame), exactly like Slack/Discord. Only ``reacted_by_me`` is
    viewer-dependent, and the viewer can never block themselves.

    Per-emoji rows are ordered ``(-count, emoji)`` — most-reacted first, ties broken by
    the opaque emoji string for a stable, deterministic wire order."""
    if not message_ids:
        return {}
    counts = (await session.execute(
        select(MessageReaction.message_id, MessageReaction.emoji, func.count())
        .where(MessageReaction.message_id.in_(message_ids))
        .group_by(MessageReaction.message_id, MessageReaction.emoji))).all()
    mine = {
        (m, e) for m, e in (await session.execute(
            select(MessageReaction.message_id, MessageReaction.emoji)
            .where(
                MessageReaction.message_id.in_(message_ids),
                MessageReaction.user_id == viewer_id,
            ))).all()
    }
    by_msg: dict[str, list[dict]] = {}
    for message_id, emoji, count in counts:
        by_msg.setdefault(message_id, []).append({
            "emoji": emoji,
            "count": count,
            "reacted_by_me": (message_id, emoji) in mine,
        })
    for message_id, entries in by_msg.items():
        entries.sort(key=lambda e: (-e["count"], e["emoji"]))
        # Truncate the long tail so a multi-user emoji raid can't bloat one message's
        # wire array — top-N by count, the band anyone actually renders.
        if len(entries) > MAX_EMOJIS_PROJECTED_PER_MESSAGE:
            by_msg[message_id] = entries[:MAX_EMOJIS_PROJECTED_PER_MESSAGE]
    return by_msg


async def purge_user_reactions(session: AsyncSession, user_id: str) -> None:
    """Delete every reaction authored by ``user_id`` — the account-deletion cascade
    teardown for this FK-to-``users`` child (children-before-parent, no ON DELETE
    CASCADE; the cascade guard requires it). Caller owns the transaction/commit,
    like the other ``purge_user_*`` services.

    NO ``reaction`` frames are emitted for the purged rows (cage-match Tesla): account
    deletion is a COLD-RELOAD event, the state-not-event tradeoff at N-message blast
    radius. Live clients' counts on every message the deleted user touched self-heal on
    the next re-page of each, exactly like any other reaction change — the app contract
    treats reaction freshness as re-page-driven, never watermark/reconnect-driven, so
    this needs no per-row repair delta (that would be a whole reactions-reset channel
    for an ambient signal). Named, not silent."""
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
