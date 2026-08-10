"""Acceptance tests for direct messages as 1:1 (member-set) channels (#2633).

Three layers, matching the design of record (docs/design/11-direct-messages.md):

  * SERVICE — find-or-create idempotency, canonical (order-independent) pairing, the
    self-DM (notes-to-self) case, member-SET (not 2-capped) shape, TargetNotFound, and
    the concurrent double-tap converging on ONE channel — through the single door
    (dm_service).
  * ROUTE — auth, the members-array wire shape, idempotent POST, 404 for a bad target,
    GET /v1/dm listing + last_message visibility, DM EXCLUSION from GET /v1/channels
    (while still readable by a member), and GET /v1/messages/{id} existence-hiding.
  * PRIVACY (the crux) — a DM send NEVER publishes to the shared bus (design §Decision
    3), while a normal channel send still does. The control pair proves the gate.

Built from JUST the dm/channels/messages routers (never `aiko_gateway.main`, which
imports the aiko bus — the suite's "never import aiko_services" isolation invariant).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sqlalchemy.exc import IntegrityError

from aiko_gateway.aiko.payload import InboundMessage
from aiko_gateway.domain import (
    acl, channels_service, dm_service, messages_service, moderation_service,
    security, users_service,
)
from aiko_gateway.domain.models import Channel, ChannelKind, Membership, Message
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.realtime.ws import _handle_send
from aiko_gateway.rest import channels as channel_routes
from aiko_gateway.rest import dm as dm_routes
from aiko_gateway.rest import members as member_routes
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def _post_msg(session, *, channel_id: str, sender, body: str, mid: int) -> Message:
    """Insert a persisted message directly (bypassing the WS path) for read tests."""
    msg = Message(
        id=_ulid(mid), channel_id=channel_id, sender_user_id=sender.id,
        sender_kind="human", sender_label=sender.display_name, body=body,
        aiko_origin=False, created_at=dt.datetime.now(dt.timezone.utc))
    session.add(msg)
    await session.commit()
    return msg


# =============================== SERVICE LAYER ============================== #

async def test_create_dm_shape(session):
    """A fresh DM is a kind='dm', private, community-less channel with a membership per
    participant — the member-SET model (no 2-cap, no member_a/member_b)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, members = await dm_service.get_or_create_dm(
        session, me=a, target_user_id=b.id)
    assert channel.kind == "dm"
    assert channel.is_private is True
    assert channel.community_id is None, "a DM must be community-less (explicit None)"
    assert members == sorted([a.id, b.id])
    rows = (await session.execute(
        Membership.__table__.select().where(
            Membership.channel_id == channel.id))).all()
    assert {r.user_id for r in rows} == {a.id, b.id}
    assert all(r.can_post for r in rows), "both DM members must be able to post"


async def test_find_or_create_is_idempotent_and_order_independent(session):
    """The unordered pair {a,b} always resolves to the SAME channel — from either
    direction, on repeat — never a second channel."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    c1, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    c2, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    c3, _ = await dm_service.get_or_create_dm(session, me=b, target_user_id=a.id)
    assert c1.id == c2.id == c3.id
    all_dms = (await session.execute(
        Channel.__table__.select().where(Channel.kind == "dm"))).all()
    assert len(all_dms) == 1, "no duplicate DM channel for the same pair"


async def test_self_dm_is_allowed_notes_to_self(session):
    """target == me resolves to a single-member notes-to-self channel (app-tab
    decision: ALLOW, not 400)."""
    a = await _user(session, "alice")
    channel, members = await dm_service.get_or_create_dm(
        session, me=a, target_user_id=a.id)
    assert channel.kind == "dm"
    assert members == [a.id], "a self-DM has exactly one member"
    rows = (await session.execute(
        Membership.__table__.select().where(
            Membership.channel_id == channel.id))).all()
    assert len(rows) == 1


async def test_target_not_found_raises(session):
    a = await _user(session, "alice")
    with pytest.raises(dm_service.TargetNotFound):
        await dm_service.get_or_create_dm(
            session, me=a, target_user_id=_ulid(999))


async def test_list_dms_only_mine(session):
    """list_dms returns only the DMs the viewer belongs to — never someone else's."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    c = await _user(session, "carol")
    ab, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    bc, _ = await dm_service.get_or_create_dm(session, me=b, target_user_id=c.id)
    a_dms = {ch.id for ch in await dm_service.list_dms(session, a.id)}
    assert a_dms == {ab.id}, "alice sees only her DM with bob, not bob<->carol"


# ================================ ROUTE LAYER ============================== #

@pytest_asyncio.fixture
async def app_ctx(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(dm_routes.router)
    app.include_router(channel_routes.router)
    app.include_router(member_routes.router)
    app.include_router(message_routes.router)
    register_error_handlers(app)
    app.state.gw = SimpleNamespace(hub=Hub())
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_post_dm_requires_auth(app_ctx):
    resp = await app_ctx.post("/v1/dm", json={"target_user_id": _ulid(1)})
    assert resp.status_code == 401


async def test_post_dm_returns_members_array(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    resp = await app_ctx.post(
        "/v1/dm", json={"target_user_id": b.id}, headers=_auth(a))
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "dm"
    assert body["members"] == sorted([a.id, b.id])
    assert isinstance(body["members"], list), "members is an ARRAY, never a peer scalar"
    assert "channel_id" in body and "created_at" in body


async def test_post_dm_is_idempotent_over_http(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    r1 = await app_ctx.post("/v1/dm", json={"target_user_id": b.id}, headers=_auth(a))
    r2 = await app_ctx.post("/v1/dm", json={"target_user_id": b.id}, headers=_auth(a))
    assert r1.json()["channel_id"] == r2.json()["channel_id"]


async def test_post_dm_bad_target_is_404(app_ctx, session):
    a = await _user(session, "alice")
    resp = await app_ctx.post(
        "/v1/dm", json={"target_user_id": _ulid(999)}, headers=_auth(a))
    assert resp.status_code == 404


async def test_get_dm_lists_with_last_message(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    # No messages yet → last_message is null.
    resp = await app_ctx.get("/v1/dm", headers=_auth(a))
    assert resp.status_code == 200
    items = resp.json()["channels"]
    assert len(items) == 1
    assert items[0]["last_message"] is None
    # After a message, last_message is populated with that message.
    await _post_msg(session, channel_id=channel.id, sender=a, body="hi bob", mid=5)
    resp = await app_ctx.get("/v1/dm", headers=_auth(a))
    last = resp.json()["channels"][0]["last_message"]
    assert last is not None and last["body"] == "hi bob"


async def test_dm_excluded_from_channels_list_but_readable_by_member(app_ctx, session):
    """A DM is kept OUT of GET /v1/channels, yet a member can still read its history
    (the exclusion is on the listing only, not the readable predicate)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    await _post_msg(session, channel_id=channel.id, sender=a, body="hi", mid=5)

    listing = await app_ctx.get("/v1/channels", headers=_auth(a))
    ids = [c["id"] for c in listing.json()["channels"]]
    assert channel.id not in ids, "DM must not appear in GET /v1/channels"

    # ...but a member reads its history fine (readable_channel untouched).
    hist = await app_ctx.get(
        f"/v1/channels/{channel.id}/messages", headers=_auth(a))
    assert hist.status_code == 200
    assert [m["body"] for m in hist.json()["messages"]] == ["hi"]


async def test_dm_history_404_for_non_member(app_ctx, session):
    """A non-member gets the existence-hiding 404 for a DM's history (private channel
    they don't belong to == no such channel)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    intruder = await _user(session, "mallory")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    resp = await app_ctx.get(
        f"/v1/channels/{channel.id}/messages", headers=_auth(intruder))
    assert resp.status_code == 404


# ------------------------- GET /v1/messages/{id} --------------------------- #

async def test_get_message_visible_to_member(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    msg = await _post_msg(session, channel_id=channel.id, sender=a, body="quote me", mid=7)
    resp = await app_ctx.get(f"/v1/messages/{msg.id}", headers=_auth(b))
    assert resp.status_code == 200
    assert resp.json()["body"] == "quote me"


async def test_get_message_404_for_non_member(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    intruder = await _user(session, "mallory")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    msg = await _post_msg(session, channel_id=channel.id, sender=a, body="secret", mid=7)
    resp = await app_ctx.get(f"/v1/messages/{msg.id}", headers=_auth(intruder))
    assert resp.status_code == 404, "existence-hiding: non-member can't fetch by id"


async def test_get_message_404_when_soft_deleted(app_ctx, session):
    """A taken-down parent is a 404 — its body is never resurrected (no retraction
    leak), the exact hazard the contract warns about for reply previews."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    msg = await _post_msg(session, channel_id=channel.id, sender=a, body="gone", mid=7)
    msg.deleted_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()
    resp = await app_ctx.get(f"/v1/messages/{msg.id}", headers=_auth(b))
    assert resp.status_code == 404


async def test_get_message_missing_is_404(app_ctx, session):
    a = await _user(session, "alice")
    resp = await app_ctx.get(f"/v1/messages/{_ulid(404)}", headers=_auth(a))
    assert resp.status_code == 404


async def test_get_message_404_when_author_blocked(app_ctx, session):
    """GET /v1/messages/{id} honours the block content-filter: a message whose author is
    in a block relationship with the viewer collapses to the same 404 (existence-hiding),
    so the fetch-by-id surface can't be used to read around a block."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    msg = await _post_msg(session, channel_id=channel.id, sender=a, body="hi", mid=7)
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    resp = await app_ctx.get(f"/v1/messages/{msg.id}", headers=_auth(b))
    assert resp.status_code == 404


async def test_dm_with_community_rejected_by_check(session):
    """ck_channels_community_required is bidirectional (#2633 cage-match): a DM
    (kind='dm') MUST have a NULL community — storing one WITH a community is rejected at
    the DB, so a DM can never be smuggled into a community's channel listing."""
    from aiko_gateway.domain.models import DEFAULT_COMMUNITY_ID
    session.add(Channel(id=_ulid(3), name="leak", kind="dm", aiko_channel="dm:leak",
                        is_private=True, community_id=DEFAULT_COMMUNITY_ID))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# ---------------- feature-interaction safety properties -------------------- #

async def test_dm_cannot_be_expanded_via_members_api(app_ctx, session):
    """A DM's membership is a HARD-sealed pair: add_member is refused with 409
    (DmMembershipImmutable), checked BEFORE the admin gate so the seal is reachable and
    not dead code behind NotChannelAdmin (cage-match PR#124 Tesla P1b)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    intruder = await _user(session, "mallory")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    resp = await app_ctx.post(
        f"/v1/channels/{channel.id}/members",
        json={"user_id": intruder.id}, headers=_auth(a))
    assert resp.status_code == 409, "a DM's membership is fixed — 409, not a growable set"
    members = await dm_service.members_of(session, channel.id)
    assert intruder.id not in members


async def test_dm_cannot_be_left(app_ctx, session):
    """Leaving a DM is refused (409) — DM membership is permanent, so the peer's next
    POST /v1/dm can't re-inject a member who left (the forced-re-add vector, Tesla P0)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    resp = await app_ctx.delete(
        f"/v1/channels/{channel.id}/leave", headers=_auth(b))
    assert resp.status_code == 409
    # b is still a member (couldn't leave) — no shell, no re-inject race.
    assert b.id in await dm_service.members_of(session, channel.id)


async def test_dm_message_hidden_from_blocked_peer(app_ctx, session):
    """The READ-side block filter still covers a DM: a message already in the channel
    (here inserted directly, bypassing the send gate) is hidden from a peer who blocks its
    author. Complements the SEND-side refusal (test_dm_send_under_block_is_refused) — the
    read filter remains the backstop for anything persisted before a block."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    await _post_msg(session, channel_id=channel.id, sender=a, body="hi", mid=5)
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    hist = await app_ctx.get(
        f"/v1/channels/{channel.id}/messages", headers=_auth(b))
    assert hist.status_code == 200
    assert hist.json()["messages"] == [], "a blocked peer's DM line is filtered from view"


# ------------- namespace-collision + reconcile reservation ----------------- #

async def test_dm_key_collision_fails_closed(session):
    """If the deterministic dm:<lo>:<hi> key is squatted by a NON-DM channel,
    get_or_create_dm FAILS CLOSED (never adopts/federates a foreign channel as a DM)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    lo, hi = sorted([a.id, b.id])
    squat = Channel(id=_ulid(1), name="squat", kind="standard",
                    aiko_channel=f"dm:{lo}:{hi}", is_private=False)
    session.add(squat)
    await session.commit()
    with pytest.raises(dm_service.DmKeyCollision):
        await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    # The squatting channel was NOT mutated — no DM members grafted onto it.
    members = await dm_service.members_of(session, squat.id)
    assert members == []


async def test_post_dm_key_collision_returns_409(app_ctx, session):
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    lo, hi = sorted([a.id, b.id])
    session.add(Channel(id=_ulid(1), name="squat", kind="standard",
                        aiko_channel=f"dm:{lo}:{hi}", is_private=False))
    await session.commit()
    resp = await app_ctx.post("/v1/dm", json={"target_user_id": b.id}, headers=_auth(a))
    assert resp.status_code == 409


async def test_reconcile_refuses_to_mint_dm_channel(session):
    """A bus channel_list naming a dm: channel must NOT be minted (upsert raises), so
    the bus can never create a channel that would shadow / collide a local DM."""
    with pytest.raises(channels_service.ReservedDmChannel):
        await channels_service.upsert_channel(session, "dm:aaa:bbb")


async def test_reconcile_refuses_to_hard_delete_dm_channel(session):
    """A bus `remove` naming a dm: channel is a safe no-op — it must NEVER hard-delete a
    private DM (+ its messages + memberships)."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    deleted = await channels_service.hard_delete_channel(session, channel.aiko_channel)
    assert deleted is False
    # The DM channel + its membership survive.
    assert (await session.get(Channel, channel.id)) is not None
    assert await dm_service.members_of(session, channel.id) == sorted([a.id, b.id])


async def test_bus_message_for_dm_channel_is_dropped(session):
    """persist_inbound drops a bus message named for a reserved dm: channel — never
    persist federated traffic into a private DM."""
    row = await messages_service.persist_inbound(session, InboundMessage(
        username="someone", channel="dm:aaa:bbb", timestamp=None, message="leak?",
        raw="(message ...)"))
    assert row is None


async def test_channel_kind_check_constraint_rejects_bogus_kind(session):
    """The ck_channels_kind DB CHECK makes an out-of-set kind unrepresentable — the
    privacy gate (kind != 'dm') can't be bypassed by a bad writer storing a novel kind."""
    session.add(Channel(id=_ulid(2), name="x", kind="totally_bogus",
                        aiko_channel="x", is_private=False))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_channel_kind_enum_values(session):
    """ChannelKind is the closed set backing the CHECK; 'dm' and 'standard' are members
    (guards against the enum silently drifting from the migration literal)."""
    assert ChannelKind.DM == "dm"
    assert ChannelKind.STANDARD == "standard"
    # 'group' is deliberately NOT pre-permitted (added with its community rule when
    # groups ship — cage-match PR#124 Tesla).
    assert {"standard", "llm", "robot", "dm"} == {k.value for k in ChannelKind}


async def test_create_outbound_refuses_blocked_dm_at_mutator_door(session):
    """The DM-block-send gate lives in the MUTATOR (create_outbound), not the route — so
    ANY send path is sealed (cage-match PR#124 Tesla P1a). Calling it directly for a
    blocked DM raises BlockedDmSend and persists nothing."""
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    with pytest.raises(messages_service.BlockedDmSend):
        await messages_service.create_outbound(
            session, user=a, channel=channel, body="x", client_msg_id="c1")
    rows = (await session.execute(
        Message.__table__.select().where(Message.channel_id == channel.id))).all()
    assert rows == []


async def test_self_join_refuses_dm(session):
    """A DM is never self-joinable — its membership is fixed at creation (hard seal,
    cage-match PR#124 Tesla P2), independent of the 'DMs are invite_only' convention."""
    from aiko_gateway.domain import memberships_service
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    with pytest.raises(memberships_service.DmMembershipImmutable):
        await memberships_service.self_join(
            session, channel_id=channel.id, actor_id=a.id)


async def test_dm_retry_after_block_returns_existing_row(ws_ctx):
    """Idempotency survives a block established AFTER the original send (cage-match PR#124
    Carnot F2): a legitimately-sent message resent with the same client_msg_id returns the
    existing row (reconciles the ack), NOT BlockedDmSend — the block stops NEW residue,
    not reconciliation of an already-sent message."""
    session = ws_ctx
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    first, created = await messages_service.create_outbound(
        session, user=a, channel=channel, body="hi", client_msg_id="c1")
    assert created
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    # Retry of the SAME client_msg_id after the block → existing row, not BlockedDmSend.
    again, created2 = await messages_service.create_outbound(
        session, user=a, channel=channel, body="hi", client_msg_id="c1")
    assert again.id == first.id and created2 is False


# ============================ PRIVACY (the crux) =========================== #

class _FakeBus:
    """Records bus publishes so a test can assert a DM never crosses the wire."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, username: str, channel: str, body: str) -> None:
        self.sent.append((username, channel, body))


class _RecordingConn(Connection):
    def __init__(self, user_id: str) -> None:
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


@pytest_asyncio.fixture
async def ws_ctx(monkeypatch):
    """A shared-DB context for the WS send path. ``_handle_send`` opens its OWN
    ``SessionLocal()`` (it does not take an injected session like ``_handle_subscribe``),
    so testing it against the in-memory fixture requires a StaticPool engine — one shared
    connection so a session opened INSIDE ``_handle_send`` sees rows a seeding session
    committed — patched over ``ws.SessionLocal``. Yields a seeding session."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession as _AS, async_sessionmaker, create_async_engine)
    from sqlalchemy.pool import StaticPool

    from aiko_gateway.db import Base
    from aiko_gateway.realtime import ws as ws_module

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=_AS, expire_on_commit=False)
    monkeypatch.setattr(ws_module, "SessionLocal", maker)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_dm_send_does_not_publish_to_bus(ws_ctx):
    """THE privacy invariant: a message sent to a DM channel is persisted + fanned out
    LOCALLY but NEVER published to the shared ChatServer bus."""
    session = ws_ctx
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    bus = _FakeBus()
    gw = SimpleNamespace(bus=bus, hub=Hub())
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": channel.id,
        "client_msg_id": "c1", "body": "our secret"})
    assert bus.sent == [], "a DM must NEVER be published to the bus"
    # ...but the sender still got an ack (the message was persisted locally).
    assert any(f.get("type") == "ack" for f in conn.sent)
    persisted = (await session.execute(
        Message.__table__.select().where(Message.channel_id == channel.id))).all()
    assert len(persisted) == 1, "the DM message is persisted island-locally"


async def test_dm_send_under_block_is_refused(ws_ctx):
    """Decision 5 (Nick's ruling 2026-08-10): a DM send under a block is REFUSED, not
    filtered — so NO dead-storage residue accrues (nothing to resurface on unblock). The
    refusal collapses into the existence-hiding 'no_channel' error, so it never leaks the
    block direction."""
    session = ws_ctx
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    conn = _RecordingConn(a.id)
    await _handle_send(SimpleNamespace(bus=_FakeBus(), hub=Hub()), conn, a, {
        "type": "send", "channel_id": channel.id,
        "client_msg_id": "c1", "body": "should not persist"})
    # Refused with the existence-hiding shape — no direction leak.
    assert any(f.get("type") == "error" and f.get("code") == "no_channel"
               for f in conn.sent)
    # Nothing persisted — no residue to resurface on a later unblock.
    persisted = (await session.execute(
        Message.__table__.select().where(Message.channel_id == channel.id))).all()
    assert len(persisted) == 0
    # It refuses in BOTH block directions (blocker A→B and blocked-by B→A): symmetric.
    conn2 = _RecordingConn(b.id)
    await _handle_send(SimpleNamespace(bus=_FakeBus(), hub=Hub()), conn2, b, {
        "type": "send", "channel_id": channel.id,
        "client_msg_id": "c2", "body": "also refused"})
    assert any(f.get("type") == "error" and f.get("code") == "no_channel"
               for f in conn2.sent)


async def test_dm_reply_under_block_uses_no_channel_not_blocked(ws_ctx):
    """A DM reply under a block refuses with the SAME existence-hiding no_channel as a
    plain send — NOT the distinct 'blocked' code the generic reply-block path emits
    (cage-match PR#124 Carnot F1/Tesla P1: one input, reply_to, must not split the error
    surface Decision 5 promised was uniform)."""
    session = ws_ctx
    a = await _user(session, "alice")
    b = await _user(session, "bob")
    channel, _ = await dm_service.get_or_create_dm(session, me=a, target_user_id=b.id)
    m1 = await _post_msg(session, channel_id=channel.id, sender=a, body="earlier", mid=5)
    await moderation_service.block_user(session, blocker_id=b.id, blocked_id=a.id)
    conn = _RecordingConn(b.id)
    await _handle_send(SimpleNamespace(bus=_FakeBus(), hub=Hub()), conn, b, {
        "type": "send", "channel_id": channel.id, "client_msg_id": "c1",
        "body": "reply under block", "reply_to": m1.id})
    codes = [f.get("code") for f in conn.sent if f.get("type") == "error"]
    assert codes == ["no_channel"], f"reply-under-block must be no_channel, got {codes}"


async def test_standard_channel_send_still_publishes_to_bus(ws_ctx):
    """The control: a normal channel send DOES publish to the bus — proving the gate
    keys on kind, not a blanket suppression."""
    session = ws_ctx
    a = await _user(session, "alice")
    ch = Channel(id=_ulid(0), name="general", kind="standard",
                 aiko_channel="general", is_private=False)
    session.add(ch)
    await session.commit()
    bus = _FakeBus()
    gw = SimpleNamespace(bus=bus, hub=Hub())
    conn = _RecordingConn(a.id)
    await _handle_send(gw, conn, a, {
        "type": "send", "channel_id": ch.id,
        "client_msg_id": "c1", "body": "hello world"})
    assert bus.sent == [(a.aiko_username, "general", "hello world")]
