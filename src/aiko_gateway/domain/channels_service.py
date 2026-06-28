"""Channel topology reconcile — mirror aiko's canonical channels into the DB.

The gateway no longer independently seeds channels (the old `_seed_channels`).
Instead it subscribes to aiko ChatServer's `channel_list` EC share and reconciles
the canonical set into local `Channel` rows. This module is the single source
for that reconcile, mirroring how `memberships_service`/`messages_service` own
their mutations — so the topology rules can't drift between call sites.

Design: docs/design/01-channel-topology-reconcile.html (#1281 incr 2).

Two operations. **Transaction ownership: these FLUSH, they do not COMMIT** — the
caller owns the transaction boundary (cage-match PR#12, Carnot P1b: a service
that commits internally breaks the caller's atomicity and would commit a broader
in-flight transaction behind its back). So `persist_inbound` can upsert a channel
+ insert the message in ONE atomic commit, and the reconcile worker owns its own
commit per event.

  * `upsert_channel`  — idempotent existence (add/update events)
  * `hard_delete_channel` — Decision A: application-level cascade
        (memberships -> messages -> channel) within the CALLER's transaction.
        Backend-agnostic: it does NOT rely on `ondelete=CASCADE` or SQLite's
        `foreign_keys` pragma (the schema has neither), so a raw `DELETE channels`
        would either IntegrityError on Postgres or orphan messages on SQLite.
        IRREVERSIBLE — callers MUST only invoke it on a live-producer EC `remove`
        (Decision B), never on ChatServer disconnect, and the reconcile worker
        serializes events so an add/remove pair can't race (Carnot P1a).

Name -> Channel fields matches the retired `_seed_channels` exactly: HyperSpace
channels are public existence; private/ACL stays a gateway-local overlay.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Channel, Membership, Message

# Pure `channel_list` EC-share parsing (CHANNEL_LIST_KEY, parse_channel_names,
# channel_name_from_item) moved to aiko/topology.py (#7) so the bus client can
# reach it without importing this DB-bound module.


async def upsert_channel(session: AsyncSession, aiko_channel: str) -> Channel:
    """Ensure a `Channel` row exists for `aiko_channel`; return it. Idempotent —
    an existing row is returned untouched (never clobbered: it may carry
    messages / a private flag set by the gateway-local overlay)."""
    existing = (await session.execute(
        select(Channel).where(Channel.aiko_channel == aiko_channel)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    channel = Channel(
        name=aiko_channel, kind="standard",
        aiko_channel=aiko_channel, is_private=False,
    )
    session.add(channel)
    await session.flush()  # caller owns commit (see module docstring)
    return channel


async def hard_delete_channel(session: AsyncSession, aiko_channel: str) -> bool:
    """IRREVERSIBLE application-level cascade: delete the channel plus ALL of its
    memberships and messages in one transaction. Returns True iff a channel was
    found and deleted (False is the safe no-op for an already-absent channel).

    Children are deleted before the parent so the operation is correct whether or
    not foreign keys are enforced. Only call on a live-producer EC `remove`.
    """
    channel = (await session.execute(
        select(Channel).where(Channel.aiko_channel == aiko_channel)
    )).scalar_one_or_none()
    if channel is None:
        return False
    await session.execute(delete(Membership).where(Membership.channel_id == channel.id))
    await session.execute(delete(Message).where(Message.channel_id == channel.id))
    await session.execute(delete(Channel).where(Channel.id == channel.id))
    await session.flush()  # caller owns commit (see module docstring)
    return True
