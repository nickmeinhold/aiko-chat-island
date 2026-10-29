"""A stranger's call invitation is held until it cannot ring (design 21).

Nick, 2026-10-29: *withhold, and keep the missed call.* The tests come in the
must-fail triple the app tab's branch `feat/ring-no-prior-conduct` set out, where
each arm reddens if exactly one conjunct of the rule is removed:

  * a stranger's FRESH invite is hidden on every door (fails if the hold is deleted);
  * THE CONTROL: once the recipient has posted, it is visible at once (fails against
    a gate that holds every invite, which would be an outage of calling);
  * a stranger's invite PAST its release is visible and is sent to the recipient
    (fails against a gate that withholds forever, which drops the missed call).

REAL ULIDS, PINNED CLOCK. Other suites mint ids as zero-padded integers, which decode
to a time near 1970: the hold reads arrival time off the id, so such a row is always
"long released" and a hold test built on it would pass against a hold that never
fires. Every row here is minted at a known instant, and `call_hold.now_ms` is pinned.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from aiko_gateway.domain import (
    call_hold, dm_service, messages_service, moderation_service, push_service,
    users_service,
)
from aiko_gateway.domain.ids import new_ulid
from aiko_gateway.domain.models import Message
from aiko_gateway.realtime.call_release import release_once
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.realtime.ws import _handle_send, _handle_subscribe

CALL_ID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
INVITE = f"aiko:call/2 {CALL_ID} · 📞 started a call"
END = f"aiko:call/2 {CALL_ID} · 📞 ended the call"
T0 = call_hold.now_ms()          # a real instant, so the scan floor sees these rows
HELD = T0 + 1_000                # one second after the call: still ringable
RELEASED = T0 + call_hold.HOLD_MS + 1


class _Conn(Connection):
    def __init__(self, user_id: str, subscribed: set[str] = frozenset()) -> None:
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed = set(subscribed)
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    def message_ids(self) -> list[str]:
        return [f["msg"]["msg_id"] for f in self.sent if f.get("type") == "message"]


@pytest.fixture
def clock(monkeypatch):
    now = {"ms": HELD}
    monkeypatch.setattr(call_hold, "now_ms", lambda: now["ms"])
    return now


@pytest_asyncio.fixture
async def dm(session):
    """alice (the caller) and bob (the recipient), in a DM where bob has NOT posted."""
    alice = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    bob = await users_service.create_user(
        session, username="bob", display_name="Bob", password="pw")
    channel, _ = await dm_service.get_or_create_dm(session, me=alice, target_user_id=bob.id)
    return SimpleNamespace(alice=alice, bob=bob, channel=channel)


async def _post(session, dm, sender, body: str, at_ms: int, *,
                kind: str = "human", origin: dict | None = None) -> Message:
    row = Message(
        id=new_ulid(at_ms), channel_id=dm.channel.id, sender_user_id=sender.id,
        sender_kind=kind, sender_label=sender.display_name, body=body,
        aiko_origin=False, origin=origin,
        created_at=dt.datetime.fromtimestamp(at_ms / 1000, dt.timezone.utc))
    session.add(row)
    await session.commit()
    return row


async def _doors(session, dm, viewer, msg) -> dict[str, bool]:
    """Can `viewer` see `msg` through each read door? One dict, so an assertion names
    the door that leaked."""
    cid = dm.channel.id
    history = await messages_service.get_history(session, cid, viewer.id, limit=50)
    last = await messages_service.last_visible_message(session, cid, viewer.id)
    lasts = await messages_service.last_visible_messages(session, [cid], viewer.id)
    return {
        "history": msg.id in {r.id for r in history},
        "fence": await messages_service.latest_ulid(session, cid, viewer.id) >= msg.id,
        "dm_preview": last is not None and last.id == msg.id,
        "dm_list": cid in lasts and lasts[cid].id == msg.id,
        "by_id": await messages_service.visible_message(session, viewer.id, msg.id)
        is not None,
    }


# ------------------------------- the triple -------------------------------- #

async def test_a_strangers_fresh_invite_is_hidden_on_every_door(session, dm, clock):
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    doors = await _doors(session, dm, dm.bob, invite)
    assert not any(doors.values()), f"a stranger's live call leaked through: {doors}"
    assert await call_hold.held_viewers(session, invite) == {dm.bob.id}, (
        "the live fanout would still reach bob's open app")


async def test_the_control_once_the_recipient_has_posted_it_is_visible(session, dm, clock):
    await _post(session, dm, dm.bob, "hi alice", T0 - 5_000)
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    doors = await _doors(session, dm, dm.bob, invite)
    assert all(doors.values()), (
        f"an established DM's call was held, which is an outage of calling: {doors}")
    assert await call_hold.held_viewers(session, invite) == set()


async def test_past_its_release_the_missed_call_is_visible_and_is_sent(session, dm, clock):
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    clock["ms"] = RELEASED
    doors = await _doors(session, dm, dm.bob, invite)
    assert all(doors.values()), f"the missed call was withheld for good: {doors}"

    hub = Hub()
    bob = _Conn(dm.bob.id, {dm.channel.id})
    alice = _Conn(dm.alice.id, {dm.channel.id})
    hub.register(bob)
    hub.register(alice)
    sent = await release_once(hub, session, after_ms=HELD, upto_ms=RELEASED)
    assert sent == 1 and bob.message_ids() == [invite.id], "the release must reach bob"
    assert alice.message_ids() == [], "the caller already has it; it goes only to bob"


# ------------------------------ the rule's edges ----------------------------- #

async def test_a_reply_during_the_hold_does_not_release_it(session, dm, clock):
    """Conduct is decided by ORDER: bob replying after the call lets the NEXT call
    through, and this one stays a missed call."""
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    await _post(session, dm, dm.bob, "who is this?", T0 + 500)
    assert not (await _doors(session, dm, dm.bob, invite))["history"]


async def test_an_agent_is_not_held(session, dm, clock):
    """Conduct is asked of persons; a non-person needs the handset's consent."""
    invite = await _post(session, dm, dm.alice, INVITE, T0, kind="agent")
    assert all((await _doors(session, dm, dm.bob, invite)).values())
    # The LIVE door separately: the read doors' candidate query also filters on
    # kind, so they cannot see `_holdable` lose its person check, and the send
    # path's `held_viewers` has no other guard (found by mutation: the read arm
    # alone let "persons-only dropped" survive).
    assert await call_hold.held_viewers(session, invite) == set()


async def test_an_end_is_not_held(session, dm, clock):
    end = await _post(session, dm, dm.alice, END, T0)
    assert all((await _doors(session, dm, dm.bob, end)).values())


async def test_an_ordinary_message_from_a_stranger_is_not_held(session, dm, clock):
    hello = await _post(session, dm, dm.alice, "hello", T0)
    assert all((await _doors(session, dm, dm.bob, hello)).values())


async def test_the_caller_always_sees_their_own_invite(session, dm, clock):
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    assert all((await _doors(session, dm, dm.alice, invite)).values())


async def test_a_block_made_during_the_hold_wins_over_the_release(session, dm, clock):
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    await moderation_service.block_user(session, dm.bob.id, dm.alice.id)
    assert await call_hold.due_releases(
        session, after_ms=HELD, upto_ms=RELEASED) == [], (
        "a release must not deliver a call the recipient blocked in the meantime")
    clock["ms"] = RELEASED
    assert not (await _doors(session, dm, dm.bob, invite))["history"]


async def test_the_release_is_anchored_on_a_future_signature(session, dm, clock):
    """Signed 30 s ahead (inside the cap): the app would ring it until signedAt + 10 s,
    so the hold runs from the signature, not from arrival."""
    signed = T0 + 30_000
    invite = await _post(session, dm, dm.alice, INVITE, T0, origin={"signed_at_ms": signed})
    assert call_hold.release_at_ms(invite) == signed + call_hold.HOLD_MS
    clock["ms"] = T0 + call_hold.HOLD_MS + 1   # past arrival + HOLD, before signed + HOLD
    assert not (await _doors(session, dm, dm.bob, invite))["history"]


async def test_an_unsigned_invite_is_anchored_on_arrival(session, dm, clock):
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    assert call_hold.release_at_ms(invite) == T0 + call_hold.HOLD_MS


# ------------------------------ the ingest cap ------------------------------- #

def test_the_cap_refuses_only_a_dm_invite_signed_beyond_it():
    far = {"signed_at_ms": T0 + call_hold.FUTURE_CAP_MS + 1}
    near = {"signed_at_ms": T0 + call_hold.FUTURE_CAP_MS}
    with pytest.raises(call_hold.FutureDatedInvite):
        call_hold.refuse_future_dated(channel_kind="dm", body=INVITE, origin=far, at_ms=T0)
    call_hold.refuse_future_dated(channel_kind="dm", body=INVITE, origin=near, at_ms=T0)
    call_hold.refuse_future_dated(channel_kind="dm", body="hello", origin=far, at_ms=T0)
    call_hold.refuse_future_dated(channel_kind="standard", body=INVITE, origin=far, at_ms=T0)
    call_hold.refuse_future_dated(channel_kind="dm", body=INVITE, origin=None, at_ms=T0)


@pytest.mark.parametrize("bob_has_posted", [False, True], ids=["stranger", "friend"])
async def test_the_cap_refuses_a_friend_and_a_stranger_alike(session, dm, bob_has_posted):
    """The refusal must not depend on the recipient's conduct, or the caller would
    learn it from the error (the no-refused-ring-record ruling)."""
    if bob_has_posted:
        await _post(session, dm, dm.bob, "hi", T0 - 5_000)
    far = {"signed_at_ms": call_hold.now_ms() + call_hold.FUTURE_CAP_MS + 60_000,
           "sender_pubkey": "unused", "key_version": 1}
    with pytest.raises(call_hold.FutureDatedInvite):
        await messages_service.create_outbound(
            session, user=dm.alice, channel=dm.channel, body=INVITE,
            client_msg_id="c1", origin=far)


# --------------------------- one definition of stranger ---------------------- #

async def test_the_wake_gate_is_bounded_by_the_invite_id(session, dm):
    """A reply that lands between the write and the wake must not ring a handset
    whose history holds the invite: both gates ask posts BELOW the invite."""
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    await _post(session, dm, dm.bob, "who is this?", T0 + 5)
    assert await push_service.spoke_before(
        session, channel_id=dm.channel.id, user_ids=[dm.bob.id],
        before_id=invite.id) == set()


# ------------------------------ the live doors ------------------------------- #

@pytest_asyncio.fixture
async def live(monkeypatch):
    """`_handle_send` opens its own SessionLocal; share one StaticPool engine with it
    (the pattern `test_direct_messages.ws_ctx` uses)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from aiko_gateway.db import Base
    from aiko_gateway.realtime import ws as ws_module

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ws_module, "SessionLocal", maker)
    async with maker() as s:
        alice = await users_service.create_user(
            s, username="alice", display_name="Alice", password="pw")
        bob = await users_service.create_user(
            s, username="bob", display_name="Bob", password="pw")
        channel, _ = await dm_service.get_or_create_dm(s, me=alice, target_user_id=bob.id)
        yield SimpleNamespace(session=s, alice=alice, bob=bob, channel=channel)
    await engine.dispose()


class _Bus:
    def send(self, *a) -> None:  # a DM never federates; present for the route's shape
        raise AssertionError("a DM was published to the bus")


@pytest.mark.parametrize("bob_has_posted", [False, True], ids=["stranger", "control"])
async def test_the_send_path_does_not_ring_a_strangers_open_app(live, bob_has_posted):
    """THE HARM ITSELF: the frame that rang an open app. Through the real route."""
    if bob_has_posted:
        await _handle_send(SimpleNamespace(bus=_Bus(), hub=Hub()), _Conn(live.bob.id),
                           live.bob, {"type": "send", "channel_id": live.channel.id,
                                      "client_msg_id": "b1", "body": "hi"})
    hub = Hub()
    bob = _Conn(live.bob.id, {live.channel.id})
    hub.register(bob)
    caller = _Conn(live.alice.id)
    await _handle_send(SimpleNamespace(bus=_Bus(), hub=hub), caller, live.alice, {
        "type": "send", "channel_id": live.channel.id,
        "client_msg_id": "c1", "body": INVITE})
    assert any(f["type"] == "ack" for f in caller.sent), "the caller's send must succeed"
    rang = bool(bob.message_ids())
    assert rang is bob_has_posted, (
        "a stranger's invite reached an open app" if rang
        else "an established DM's call never reached the open app")


async def test_a_future_dated_invite_is_refused_as_bad_origin(live):
    caller = _Conn(live.alice.id)
    origin = {"v": 1, "alg": "EdDSA", "key_version": 1, "client_msg_id": "c1",
              "sender_pubkey": "z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
              "signed_at_ms": call_hold.now_ms() + call_hold.FUTURE_CAP_MS + 60_000,
              "sig": "A" * 86}
    await _handle_send(SimpleNamespace(bus=_Bus(), hub=Hub()), caller, live.alice, {
        "type": "send", "channel_id": live.channel.id, "client_msg_id": "c1",
        "body": INVITE, "origin": origin})
    errors = [f for f in caller.sent if f["type"] == "error"]
    assert [e["code"] for e in errors] == ["bad_origin"], caller.sent


# ------------------------- the subscribe fence and replay -------------------- #

async def test_the_fence_is_capped_below_a_held_invite_and_the_rest_is_replayed(
        session, dm, clock):
    """App tab, 2026-10-29: a fence counting a later message L but not the held invite
    I walks the pager's watermark past I, stranding the missed call on a device that is
    offline at release. Capped below I, the rows above it are replayed live."""
    before = await _post(session, dm, dm.alice, "hey", T0 - 2_000)
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    later = await _post(session, dm, dm.alice, "pick up!", T0 + 500)
    conn = _Conn(dm.bob.id)
    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [dm.channel.id]},
                            session)
    (suback,) = [f for f in conn.sent if f["type"] == "suback"]
    fence = suback["channel_fences"][dm.channel.id]
    assert fence == before.id, f"fence must stop below the held invite: {suback}"
    assert conn.message_ids() == [later.id], (
        "the visible row above the cap must be replayed, and the held one must not")
    assert invite.id not in conn.message_ids()


async def test_first_contact_caps_the_fence_to_empty_and_still_replays(session, dm, clock):
    """The common case: the call is the DM's first row, so nothing is below it."""
    await _post(session, dm, dm.alice, INVITE, T0)
    later = await _post(session, dm, dm.alice, "it's alice, pick up", T0 + 500)
    conn = _Conn(dm.bob.id)
    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [dm.channel.id]},
                            session)
    (suback,) = [f for f in conn.sent if f["type"] == "suback"]
    assert suback["channel_fences"][dm.channel.id] == ""
    assert conn.message_ids() == [later.id]


async def test_without_a_hold_the_subscribe_is_unchanged(session, dm, clock):
    await _post(session, dm, dm.bob, "hi", T0 - 5_000)
    invite = await _post(session, dm, dm.alice, INVITE, T0)
    conn = _Conn(dm.bob.id)
    await _handle_subscribe(conn, {"type": "subscribe", "channel_ids": [dm.channel.id]},
                            session)
    (suback,) = [f for f in conn.sent if f["type"] == "suback"]
    assert suback["channel_fences"][dm.channel.id] == invite.id
    assert conn.message_ids() == [], "nothing is replayed when nothing is held"

