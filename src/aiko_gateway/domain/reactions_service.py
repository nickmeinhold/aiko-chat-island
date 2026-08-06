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

BLOCK VISIBILITY IS ONE PREDICATE ON BOTH PATHS (cage-match Carnot/Tesla). A user
never sees reactions authored by someone in a block relationship with them, and the
live fanout never reaches a subscriber who cannot see the target message. The read
half is enforced here (``aggregate_for_messages`` filters blocked reactors); the
live half is enforced by the route's fanout exclusion (reactor's ∪ message author's
block pairs). Same content-filter shape as messages (#7) — a delete/hide only ever
removes what you see.

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

from . import moderation_service
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


class InvalidEmoji(ValueError):
    """The supplied emoji is empty/blank, over-long, or carries a structural hazard
    (a path separator or control char) — a controlled 422 at the route, never an
    FK/length error at commit or an un-deletable row."""


class ReactionLimitExceeded(Exception):
    """The user already holds ``MAX_REACTIONS_PER_USER_PER_MESSAGE`` distinct emojis
    on this message — a controlled 429 at the route, not an unbounded row spray."""


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
    """Viewer-dependent reaction aggregate for a PAGE of messages, in ONE grouped
    query (+ one for the viewer's own rows, + one small block-set read) — never N+1
    per message. Returns ``{message_id: [{"emoji", "count", "reacted_by_me"}, ...]}``
    with an entry only for messages that have at least one VISIBLE reaction (absent ==
    none, so the serializer defaults to ``[]``).

    BLOCK-FILTERED (cage-match Tesla): reactions authored by a user in a block
    relationship with the viewer are excluded from both ``count`` and existence — the
    same content-hiding a blocked author's MESSAGES get (#7), applied to their
    reactions, so the read path and the live fanout enforce ONE visibility predicate
    (the fanout half lives in the route's exclusion set). The viewer's own row is never
    blocked (you cannot block yourself), so ``reacted_by_me`` is unaffected.

    Per-emoji rows are ordered ``(-count, emoji)`` — most-reacted first, ties broken by
    the opaque emoji string for a stable, deterministic wire order."""
    if not message_ids:
        return {}
    blocked = await moderation_service.blocked_pair_user_ids(session, viewer_id)
    counts_where = [MessageReaction.message_id.in_(message_ids)]
    if blocked:
        counts_where.append(MessageReaction.user_id.notin_(blocked))
    counts = (await session.execute(
        select(MessageReaction.message_id, MessageReaction.emoji, func.count())
        .where(*counts_where)
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
    for entries in by_msg.values():
        entries.sort(key=lambda e: (-e["count"], e["emoji"]))
    return by_msg


async def purge_user_reactions(session: AsyncSession, user_id: str) -> None:
    """Delete every reaction authored by ``user_id`` — the account-deletion cascade
    teardown for this FK-to-``users`` child (children-before-parent, no ON DELETE
    CASCADE; the cascade guard requires it). Caller owns the transaction/commit,
    like the other ``purge_user_*`` services."""
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
