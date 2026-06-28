"""ATDD — channel topology reconcile (#1281 incr 2).

The gateway mirrors aiko's canonical channel set (received via the
`channel_list` EC share) into local `Channel` rows, replacing the hardcoded
`_seed_channels`. Two operations, both single-writer (the aiko thread bridges to
the one asyncio loop, so no concurrency race):

  * add/update event -> `upsert_channel` (idempotent existence)
  * live remove event -> `hard_delete_channel` (application-cascade; IRREVERSIBLE)

Design: docs/design/01-channel-topology-reconcile.html. These specs pin the
destructive contract (Decision A's cascade) before the code that can destroy.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aiko_gateway.aiko import topology
from aiko_gateway.aiko.payload import InboundMessage
from aiko_gateway.domain import channels_service, messages_service
from aiko_gateway.domain.models import Channel, Membership, Message, User


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
        Message(id="M2", channel_id="C1", sender_user_id=None, sender_kind="llm", body="yo"),
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


# --- topology worker serializes events in arrival order (Carnot P1a) ------- #

@pytest.mark.asyncio
async def test_channel_worker_applies_events_in_fifo_order(monkeypatch):
    """The single worker must drain the queue FIFO so an add/remove pair for the
    same channel can never interleave across the aiko->asyncio bridge. A bare
    create_task-per-event let the remove run before the add and resurrect a
    deleted channel — the exact irreversible-path hazard the queue dissolves."""
    import asyncio

    from aiko_gateway import main

    calls: list[tuple[str, str]] = []

    async def fake_upsert(_session, name):
        await asyncio.sleep(0)  # force a scheduler yield mid-op
        calls.append(("add", name))

    async def fake_delete(_session, name):
        calls.append(("remove", name))
        return True

    class _NoSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(main.channels_service, "upsert_channel", fake_upsert)
    monkeypatch.setattr(main.channels_service, "hard_delete_channel", fake_delete)
    monkeypatch.setattr(main, "SessionLocal", lambda: _NoSession())

    st = main.GatewayState()
    st.loop = asyncio.get_running_loop()
    st._channel_events = asyncio.Queue()
    st._channel_events.put_nowait(("add", "random"))
    st._channel_events.put_nowait(("remove", "random"))

    worker = asyncio.create_task(st._run_channel_worker())
    await asyncio.wait_for(st._channel_events.join(), timeout=2)
    worker.cancel()

    # add MUST land before remove despite the yield inside upsert.
    assert calls == [("add", "random"), ("remove", "random")]
