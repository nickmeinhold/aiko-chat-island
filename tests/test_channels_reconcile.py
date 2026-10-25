"""ATDD — channel topology reconcile (#1281 incr 2).

The gateway mirrors aiko's canonical channel set (received via the
`channel_list` EC share) into local `Channel` rows, replacing the hardcoded
`_seed_channels`. Two operations, both single-writer (the aiko thread bridges to
the one asyncio loop, so no concurrency race):

  * add/update event -> `upsert_channel` (idempotent existence)
  * remove event -> unsubscribe ONLY; rows and history kept (Nick's #24 ruling,
    2026-10-25: bus topology controls subscriptions, never channel rows).
    `hard_delete_channel` remains the attended deletion tool, never bus-driven.

Design: docs/design/01-channel-topology-reconcile.html. These specs pin the
destructive contract (Decision A's cascade) before the code that can destroy.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aiko_gateway.aiko import topology
from aiko_gateway.aiko.payload import InboundMessage
from aiko_gateway.domain import channels_service, messages_service
from aiko_gateway.domain.models import (
    Channel, Membership, Message, MessageReport, Retraction, User)


# --- parse: EC channel_list payload -> channel names ----------------------- #

def test_parse_extracts_names_from_servicefilter_tuple():
    # Exact shape observed in spike/probe_channel_list.py.
    channel_list = {
        "general": [["*", "general", "*", "*", "*", []], "None", "None"],
        "llm": [["*", "llm", "*", "*", "*", []], "None", "None"],
    }
    assert topology.parse_channel_names(channel_list) == {"general", "llm"}


def test_parse_falls_back_to_key_when_value_unstructured():
    # If the value isn't the expected tuple, the dict KEY is the name.
    channel_list = {"random": "None", "robot": None}
    assert topology.parse_channel_names(channel_list) == {"random", "robot"}


def test_parse_empty_or_missing_is_empty_set():
    assert topology.parse_channel_names({}) == set()
    assert topology.parse_channel_names(None) == set()


# --- upsert_channel -------------------------------------------------------- #

@pytest.mark.asyncio
async def test_upsert_creates_channel_row(session):
    ch = await channels_service.upsert_channel(session, "general")
    assert ch.aiko_channel == "general"
    assert ch.name == "general"
    assert ch.kind == "standard"
    assert ch.is_private is False
    count = (await session.execute(select(func.count()).select_from(Channel))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_upsert_is_idempotent(session):
    a = await channels_service.upsert_channel(session, "general")
    b = await channels_service.upsert_channel(session, "general")
    assert a.id == b.id
    count = (await session.execute(select(func.count()).select_from(Channel))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_upsert_preserves_existing_row(session):
    # A pre-existing channel (e.g. one carrying messages) is not clobbered.
    session.add(Channel(id="C1", name="general", kind="standard",
                        aiko_channel="general", is_private=True))
    await session.commit()
    ch = await channels_service.upsert_channel(session, "general")
    assert ch.id == "C1"
    assert ch.is_private is True  # untouched


# --- hard_delete_channel (Decision A: application-level cascade) ------------ #

@pytest.mark.asyncio
async def test_hard_delete_cascades_messages_and_memberships(session):
    """The irreversible contract: delete channel + ALL its messages + memberships
    in one transaction, regardless of FK enforcement."""
    user = User(id="U1", username="alice", display_name="Alice",
                password_hash="x", aiko_username="alice")
    ch = Channel(id="C1", name="random", kind="standard", aiko_channel="random")
    session.add_all([user, ch])
    await session.flush()
    session.add_all([
        Membership(channel_id="C1", user_id="U1", role="member"),
        Message(id="M1", channel_id="C1", sender_user_id="U1", sender_kind="human", body="hi"),
        # An unidentified bus speaker is UNKNOWN (#3144 — the sender_kind closed
        # set). Was "llm", a member retired because nothing can create an llm channel.
        Message(id="M2", channel_id="C1", sender_user_id=None, sender_kind="unknown", body="yo"),
    ])
    await session.commit()

    deleted = await channels_service.hard_delete_channel(session, "random")
    assert deleted is True

    assert (await session.execute(
        select(func.count()).select_from(Channel))).scalar_one() == 0
    assert (await session.execute(
        select(func.count()).select_from(Message))).scalar_one() == 0
    assert (await session.execute(
        select(func.count()).select_from(Membership))).scalar_one() == 0
    # The user is NOT a channel-owned row — it survives.
    assert (await session.execute(
        select(func.count()).select_from(User))).scalar_one() == 1


@pytest.mark.asyncio
async def test_hard_delete_cascades_retractions_and_reports(session):
    """Everything FK'd to a channel's messages must tear down with them (else orphan
    under FK-off / refuse under FK-on): retractions (#7, FK messages + channel) AND
    message_reports (cage-match Wu — FK messages.id, the child the cascade missed)."""
    reporter = User(id="U1", username="alice", display_name="Alice",
                    password_hash="x", aiko_username="alice")
    ch = Channel(id="C1", name="random", kind="standard", aiko_channel="random")
    session.add_all([reporter, ch])
    await session.flush()
    session.add_all([
        Message(id="M1", channel_id="C1", sender_kind="human", body="bad"),
        Retraction(id="R1", target_msg_id="M1", channel_id="C1"),
        # A real member, not a placeholder: `reason="x"` here until 2026-09-26,
        # when 0028 closed the set and this insert started failing. The literal
        # was declared in no enum and survived only because nothing could object
        # — the same shape as the `sender_kind="user"` fixtures 0027 uncovered.
        # This test is about the cascade, so the value is incidental; that is
        # exactly why it was never chosen.
        MessageReport(id="RP1", message_id="M1", reporter_user_id="U1",
                      reason="spam"),
    ])
    await session.commit()

    assert await channels_service.hard_delete_channel(session, "random") is True
    for model in (Retraction, MessageReport, Message):
        assert (await session.execute(
            select(func.count()).select_from(model))).scalar_one() == 0
    # The reporter user is NOT a channel-owned row — it survives.
    assert (await session.execute(
        select(func.count()).select_from(User))).scalar_one() == 1


@pytest.mark.asyncio
async def test_hard_delete_only_targets_named_channel(session):
    session.add_all([
        Channel(id="C1", name="random", kind="standard", aiko_channel="random"),
        Channel(id="C2", name="general", kind="standard", aiko_channel="general"),
        Message(id="M1", channel_id="C2", sender_kind="human", body="keep me"),
    ])
    await session.commit()
    await channels_service.hard_delete_channel(session, "random")
    remaining = (await session.execute(select(Channel.aiko_channel))).scalars().all()
    assert remaining == ["general"]
    assert (await session.execute(
        select(func.count()).select_from(Message))).scalar_one() == 1  # general's msg kept


@pytest.mark.asyncio
async def test_hard_delete_nonexistent_is_noop(session):
    deleted = await channels_service.hard_delete_channel(session, "ghost")
    assert deleted is False


# --- reconcile_snapshot: bulk add from a full share cache ------------------ #

# --- persist_inbound closes the startup window (no _seed_channels) --------- #

@pytest.mark.asyncio
async def test_persist_inbound_autocreates_missing_channel(session):
    """An inbound bus message for a not-yet-reconciled channel is HyperSpace-
    confirmed existence -> the channel is upserted and the message persisted,
    not dropped (closes the post-seed-removal startup window)."""
    msg = InboundMessage(username="bob", channel="random", timestamp=None, message="hi", raw="hi")
    row = await messages_service.persist_inbound(session, msg)
    assert row is not None
    assert row.body == "hi"
    ch = (await session.execute(
        select(Channel).where(Channel.aiko_channel == "random"))).scalar_one()
    assert row.channel_id == ch.id


@pytest.mark.asyncio
async def test_persist_inbound_no_channel_returns_none(session):
    msg = InboundMessage(username="bob", channel=None, timestamp=None, message="x", raw="x")
    assert await messages_service.persist_inbound(session, msg) is None


# --- channel_name_from_item: EC event name -> channel name ----------------- #

def test_channel_name_from_item_strips_prefix():
    assert topology.channel_name_from_item("channel_list.general") == "general"


def test_channel_name_from_item_keeps_dotted_name():
    assert topology.channel_name_from_item("channel_list.a.b") == "a.b"


def test_channel_name_from_item_ignores_parent_and_unrelated():
    assert topology.channel_name_from_item("channel_list") is None
    assert topology.channel_name_from_item("source_file") is None
    assert topology.channel_name_from_item("") is None
    assert topology.channel_name_from_item(None) is None


# --- a bus remove never reaches the DB (#24 ruling) ------------------------ #

class _SharedSession:
    """Hands the worker the TEST's own session, so the rows these tests assert on
    are the rows the worker could touch (a worker on its own SessionLocal would
    make the row assertions decorative; cage-match PR#409, Noether)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return False


async def _drain(st):
    """Run the worker until the queue is empty, then stop it cleanly."""
    import asyncio
    import contextlib

    await asyncio.sleep(0)  # call_soon_threadsafe delivers on the next tick
    worker = asyncio.create_task(st._run_channel_worker())
    await asyncio.wait_for(st._channel_events.join(), timeout=2)
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker


def _state():
    import asyncio

    from aiko_gateway import main

    st = main.GatewayState()
    st.loop = asyncio.get_running_loop()
    st._channel_events = asyncio.Queue()
    return st


async def _channel_with_history(session):
    user = User(id="U1", username="alice", display_name="Alice",
                password_hash="x", aiko_username="alice")
    ch = Channel(id="C1", name="random", kind="standard", aiko_channel="random")
    session.add_all([user, ch])
    await session.flush()
    session.add_all([
        Membership(channel_id="C1", user_id="U1", role="member"),
        Message(id="M1", channel_id="C1", sender_user_id="U1", sender_kind="human",
                body="hi"),
    ])
    await session.commit()


async def _counts(session):
    out = []
    for m in (Channel, Message, Membership):
        out.append((await session.execute(
            select(func.count()).select_from(m))).scalar_one())
    return tuple(out)


@pytest.mark.asyncio
async def test_bus_remove_keeps_channel_messages_and_memberships(session, monkeypatch):
    """The #24 ruling at the gateway seam: a bus `remove` for a channel with history
    leaves the channel row, its messages and its memberships intact, and never
    reaches the destructive cascade. The worker runs on the test's own session, so
    a deletion by ANY path (not just hard_delete_channel) would show in the counts."""
    from aiko_gateway import main

    # Record, don't raise: the worker's `except Exception` would swallow a raise.
    delete_calls: list[str] = []

    async def recording_delete(_session, name):
        delete_calls.append(name)
        return True

    monkeypatch.setattr(main.channels_service, "hard_delete_channel", recording_delete)
    monkeypatch.setattr(main, "SessionLocal", lambda: _SharedSession(session))
    await _channel_with_history(session)

    st = _state()
    st.on_channel_remove("random")
    await _drain(st)

    assert delete_calls == []
    assert await _counts(session) == (1, 1, 1)


@pytest.mark.asyncio
async def test_bus_remove_then_readd_keeps_the_original_row(session, monkeypatch):
    """remove -> add through the real callbacks and worker: the channel comes back
    on its ORIGINAL row with its history. Under the old worker, remove hard-deleted
    and the re-add minted a new row with a new id and no messages."""
    from aiko_gateway import main

    monkeypatch.setattr(main, "SessionLocal", lambda: _SharedSession(session))
    await _channel_with_history(session)

    st = _state()
    st.on_channel_remove("random")
    st.on_channel_add("random")
    st.on_channel_add("fresh")  # proves adds actually flow through the worker
    await _drain(st)

    by_name = {r.aiko_channel: r.id for r in
               (await session.execute(select(Channel))).scalars().all()}
    assert by_name["random"] == "C1"  # original row, not a re-mint
    assert set(by_name) == {"random", "fresh"}  # without the add path, no "fresh"
    assert await _counts(session) == (2, 1, 1)


# --- topology worker applies adds in arrival order (Carnot P1a) ------------ #

@pytest.mark.asyncio
async def test_channel_worker_applies_adds_in_fifo_order(monkeypatch):
    """The single worker drains the queue FIFO. The FIRST upsert yields many times
    and the second not at all, so any concurrent drain (a task per event) would let
    the second finish first; only a serial worker keeps arrival order."""
    import asyncio

    from aiko_gateway import main

    calls: list[str] = []

    async def fake_upsert(_session, name):
        if name == "random":
            for _ in range(5):
                await asyncio.sleep(0)  # yield repeatedly mid-op
        calls.append(name)

    class _NoSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(main.channels_service, "upsert_channel", fake_upsert)
    monkeypatch.setattr(main, "SessionLocal", lambda: _NoSession())

    st = _state()
    st.on_channel_add("random")
    st.on_channel_add("general")
    await _drain(st)

    assert calls == ["random", "general"]
