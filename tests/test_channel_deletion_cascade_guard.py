"""Channel-deletion cascade COMPLETENESS guards: the channel-side twin of
`test_account_deletion_cascade_guard.py`.

`channels_service.hard_delete_channel` must tear down every row that points at
the channel, AND every row that points at one of its messages (those are deleted
too). FK is OFF in prod (ISL-0002), so nothing but this function does it. Its
"verify-the-neighbor" comment is a manual checklist, and it missed three times:
message_reports (cage-match Wu), message_reactions (#2634), then
channel_read_positions (#429). These two tests turn the checklist into a valve.

  * `test_channel_fk_set_is_the_expected_set`: a STRUCTURAL tripwire. Every column
    with a foreign key to `channels.id` or `messages.id` must be in the expected
    set. Add a table that references either and this fails first, pointing at
    `hard_delete_channel` before an orphan can reach prod.

  * `test_hard_delete_leaves_no_row_referencing_the_channel`: a BEHAVIORAL proof.
    Seeds a row in every such column, asserts each references the doomed channel
    or its messages BEFORE deletion (so it cannot pass vacuously), deletes, then
    checks generically that no such column still does.

The behavioral test alone passes vacuously for a future table nobody seeds; the
tripwire alone proves nothing about behavior. Keep both.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from aiko_gateway.db import Base
from aiko_gateway.domain import channels_service
from aiko_gateway.domain.models import (
    Channel, ChannelReadPosition, Membership, Message, MessageReaction,
    MessageReport, Retraction, User)

_PARENTS = ("channels", "messages")


def _child_fk_columns() -> set[tuple[str, str, str]]:
    """Every (table, column, parent) whose FK targets `channels.id` or
    `messages.id`."""
    found: set[tuple[str, str, str]] = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name in _PARENTS and fk.column.name == "id":
                    found.add((table.name, col.name, fk.column.table.name))
    return found


# The columns `hard_delete_channel` is KNOWN to handle. Keep this in lockstep with
# that function: adding a row here without the teardown there re-buries the bug.
EXPECTED_CHILD_FK_COLUMNS: set[tuple[str, str, str]] = {
    ("memberships", "channel_id", "channels"),             # delete
    ("messages", "channel_id", "channels"),                # delete
    ("messages", "reply_to", "messages"),                  # deleted with its channel
    ("message_retractions", "channel_id", "channels"),     # delete
    ("message_retractions", "target_msg_id", "messages"),  # delete (by channel)
    ("message_reports", "message_id", "messages"),         # delete
    ("message_reactions", "message_id", "messages"),       # purge_reactions_for_messages
    ("channel_read_positions", "channel_id", "channels"),  # delete (#429)
}


def test_channel_fk_set_is_the_expected_set():
    """If this fails, a table now references a channel or a message and
    `hard_delete_channel` may not clear it. Wire the teardown in FIRST, then add
    the column here. Do NOT just update the set to make the test green."""
    actual = _child_fk_columns()
    new = actual - EXPECTED_CHILD_FK_COLUMNS
    gone = EXPECTED_CHILD_FK_COLUMNS - actual
    assert not new, (
        f"New FK(s) into channels/messages NOT declared as cascade-handled: "
        f"{sorted(new)}. Wire each into hard_delete_channel, then add it here.")
    assert not gone, (
        f"Expected FK(s) vanished from the schema: {sorted(gone)}. Update "
        "EXPECTED_CHILD_FK_COLUMNS.")


async def test_hard_delete_leaves_no_row_referencing_the_channel(session):
    user = User(id="U".ljust(26, "0"), username="alice", display_name="Alice",
                password_hash="x", aiko_username="alice")
    doomed = Channel(id="C1".ljust(26, "0"), name="doomed", kind="standard",
                     aiko_channel="doomed")
    kept = Channel(id="C2".ljust(26, "0"), name="kept", kind="standard",
                   aiko_channel="kept")
    session.add_all([user, doomed, kept])
    await session.flush()
    now = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)
    m1, m2, mk = ("M1".ljust(26, "0"), "M2".ljust(26, "0"), "MK".ljust(26, "0"))
    session.add_all([
        Membership(channel_id=doomed.id, user_id=user.id, role="member"),
        Message(id=m1, channel_id=doomed.id, sender_user_id=user.id,
                sender_kind="human", body="hi", created_at=now),
        Message(id=m2, channel_id=doomed.id, sender_user_id=user.id,
                sender_kind="human", body="re", reply_to=m1, created_at=now),
        # A survivor in another channel, so "delete everything" cannot pass.
        Message(id=mk, channel_id=kept.id, sender_user_id=user.id,
                sender_kind="human", body="stay", created_at=now),
    ])
    await session.flush()
    session.add_all([
        Retraction(target_msg_id=m1, channel_id=doomed.id),
        MessageReport(message_id=m2, reporter_user_id=user.id, reason="spam"),
        MessageReaction(message_id=m1, user_id=user.id, emoji="x"),
        ChannelReadPosition(user_id=user.id, channel_id=doomed.id,
                            last_read=m2, seq=1),
        ChannelReadPosition(user_id=user.id, channel_id=kept.id,
                            last_read=mk, seq=1),
    ])
    await session.commit()

    doomed_ids = {"channels": {doomed.id}, "messages": {m1, m2}}

    async def referencing() -> dict[tuple[str, str], int]:
        counts = {}
        for table, col, parent in sorted(_child_fk_columns()):
            t = Base.metadata.tables[table]
            counts[(table, col)] = (await session.execute(
                select(func.count()).select_from(t).where(
                    t.c[col].in_(doomed_ids[parent])))).scalar_one()
        return counts

    before = await referencing()
    # messages.reply_to points at m1 from m2; every other column was seeded above.
    unseeded = [k for k, n in before.items() if n == 0]
    assert not unseeded, (
        f"seed a row for {unseeded}, or this test passes vacuously for them")

    assert await channels_service.hard_delete_channel(session, "doomed") is True
    await session.commit()

    after = await referencing()
    leftover = {k: n for k, n in after.items() if n}
    assert not leftover, f"rows still reference the deleted channel: {leftover}"
    # The other channel and its rows are untouched.
    assert (await session.get(Message, mk)) is not None
    kept_rp = (await session.execute(select(ChannelReadPosition).where(
        ChannelReadPosition.channel_id == kept.id))).scalars().all()
    assert len(kept_rp) == 1
