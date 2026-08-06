"""Emoji-reaction persistence + aggregation (#2634, v2 social layer).

The SINGLE mutator door for reactions (enforce-at-the-backend-through-one-door):
the REST routes, and any future in-process caller, add/remove through
``add_reaction`` / ``remove_reaction`` here, so the idempotency + validation live
in one place. Reads are the viewer-dependent ``aggregate_for_messages`` projection
folded into ``messages_service.message_view`` on the history path.

STATE, NOT EVENT — see the ``MessageReaction`` model docstring. There is no
forward-ULID reaction event and no separate feed: a reaction changes an aggregate
that ``message_view`` recomputes on every history read, so a missed live frame
self-heals on the next re-page. The live ``reaction`` WS frame
(``envelopes.reaction_frame``) is a best-effort latency optimisation over that
recomputed aggregate, exactly as WS message fanout is over ``get_history``.

CONCURRENCY (named MVP tradeoff, mirrors ``moderation_service.block_user``):
idempotency is check-then-insert, not conflict-safe. Under the single-writer SQLite
deployment there is no race. On the public-scale Postgres path two concurrent
identical reactions could both pass the ``session.get`` check and the second insert
raise IntegrityError; the robust fix is a dialect upsert (``ON CONFLICT DO
NOTHING``), tracked with the same Postgres-migration cluster (claude-tasks #14). A
rollback-and-reread is deliberately NOT used — rollback on the shared async session
raises MissingGreenlet (the trap the account-deletion PR already hit and rejected).
"""
from __future__ import annotations

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import MessageReaction

# Opaque-emoji length cap (defense-in-depth). A real emoji, incl. a ZWJ sequence
# (family/skin-tone/flag), is well under this; the cap just stops an unbounded blob
# masquerading as an emoji. Mirrors MessageReaction.emoji String(64).
MAX_EMOJI_LEN = 64


class InvalidEmoji(ValueError):
    """The supplied emoji is empty/blank or exceeds ``MAX_EMOJI_LEN`` — a controlled
    422 at the route, never an FK/length error at commit."""


def validate_emoji(emoji: object) -> str:
    """Return ``emoji`` unchanged if it is a non-blank string within the length cap,
    else raise ``InvalidEmoji``. The value is OPAQUE — never normalised or mutated
    (the app owns rendering); this only bounds it so a malformed blob can't be
    stored as a reaction."""
    if not isinstance(emoji, str):
        raise InvalidEmoji("emoji must be a string")
    if not emoji.strip():
        raise InvalidEmoji("emoji must be non-empty")
    if len(emoji) > MAX_EMOJI_LEN:
        raise InvalidEmoji(f"emoji exceeds {MAX_EMOJI_LEN} chars")
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
) -> int:
    """Idempotently add ``user_id``'s ``emoji`` reaction to ``message_id``; return the
    resulting live count for that emoji. A repeat of the same emoji by the same user
    is a no-op (the composite PK already exists) — the count is unchanged, so the
    caller still fans out the current truth.

    Message existence + visibility are the CALLER's responsibility (the route
    resolves the message through the channel ACL and rejects a soft-deleted target
    first), mirroring ``moderation_service.report_message`` — this service guards
    only the emoji shape so a bad value is a controlled 422, not an FK 500 at commit.
    ``emoji`` is assumed already ``validate_emoji``-checked by the route."""
    already = await session.get(MessageReaction, (message_id, user_id, emoji))
    if already is None:
        session.add(MessageReaction(
            message_id=message_id, user_id=user_id, emoji=emoji))
        await session.commit()
    return await _count(session, message_id, emoji)


async def remove_reaction(
    session: AsyncSession, *, user_id: str, message_id: str, emoji: str,
) -> int:
    """Remove ``user_id``'s ``emoji`` reaction from ``message_id`` (idempotent — a
    missing row deletes zero and is not an error); return the resulting live count.
    A conditional DELETE folded into one statement, no observe-then-write."""
    await session.execute(
        delete(MessageReaction).where(
            MessageReaction.message_id == message_id,
            MessageReaction.user_id == user_id,
            MessageReaction.emoji == emoji,
        ))
    await session.commit()
    return await _count(session, message_id, emoji)


async def aggregate_for_messages(
    session: AsyncSession, message_ids: list[str], viewer_id: str,
) -> dict[str, list[dict]]:
    """Viewer-dependent reaction aggregate for a PAGE of messages, in ONE grouped
    query (+ one for the viewer's own rows) — never N+1 per message. Returns
    ``{message_id: [{"emoji", "count", "reacted_by_me"}, ...]}`` with an entry only
    for messages that have at least one reaction (absent == no reactions, so the
    serializer defaults to ``[]``).

    Per-emoji rows are ordered ``(-count, emoji)`` — most-reacted first, ties broken
    by the opaque emoji string for a stable, deterministic wire order. ``reacted_by_me``
    is TRUE iff the viewer has a row for that (message, emoji): the per-viewer half
    that makes the same message serialize differently for different readers (the same
    viewer-dependence blocks already give the history stream)."""
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
