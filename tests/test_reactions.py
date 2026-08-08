"""Acceptance tests for SIGNED, IDENTITY-BEARING emoji reactions (#2634).

The rebuild after the reverted anonymous+unsigned first cut. Two layers:
  * SERVICE — idempotency, the bounded-A block-filtered identity aggregate, origin
    carriage, validation, caps, the account-deletion purge, folded through the ONE
    door (reactions_service).
  * ROUTE — auth, the existence-hiding visibility gate, origin shape-validation at
    the trust boundary, the discrete IDENTITY-delta `reaction` WS fanout (no count),
    and the block-consistent reactions[] on the history read.

Built from JUST the reaction + message routers (never `aiko_gateway.main`, which
imports the aiko bus — the suite's "never import aiko_services" isolation invariant).
The DB `get_session` dependency is overridden to the in-memory test session; a real
`Hub` is wired onto `app.state.gw` so the fanout is exercised, not mocked.

Contract under test (SIGNING-SPEC + Nick's ruling "never anonymous"):
  - reaction carries reactor `user_id` + optional signed `origin` (gateway CARRIES,
    does not verify); identity is EXPOSED on read (reactors[]), NOT an anonymous tally
  - the block predicate governs the WHOLE projection: a blocked reactor is dropped
    from reactors[] AND count, consistently on every path — no count oracle
  - `count` is authoritative; reactors[] is a bounded sample of it
  - add/remove idempotent; remove is ownership-authorised; emoji opaque + bounded
  - you may only react to a message you can SEE (same predicate as history)
"""
from __future__ import annotations

import base64
import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import (
    accounts_service, channels_service, messages_service, moderation_service,
    reactions_service, security, signing, users_service)
from aiko_gateway.domain.models import Channel, Membership, Message, MessageReaction
from aiko_gateway.realtime.envelopes import ReactionAction
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest import reactions as reaction_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers

THUMB = "\U0001f44d"   # 👍
HEART = "❤️"  # ❤️
NO_BLOCK: set[str] = set()


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _channel(session, *, cid: int = 0, name: str = "general",
                   private: bool = False) -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=private)
    session.add(ch)
    await session.commit()
    return ch


async def _msg(session, *, mid: int, channel: Channel, sender,
               deleted: bool = False) -> Message:
    msg = Message(
        id=_ulid(mid), channel_id=channel.id, sender_user_id=sender.id,
        sender_kind="human", sender_label=sender.display_name, body="hi",
        aiko_origin=False, created_at=dt.datetime.now(dt.timezone.utc),
        deleted_at=dt.datetime.now(dt.timezone.utc) if deleted else None)
    session.add(msg)
    await session.commit()
    return msg


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _signed_origin(priv: Ed25519PrivateKey, *, channel_id: str, client_msg_id: str,
                   target_msg_id: str, emoji: str, action: str = "add",
                   signed_at_ms: int = 1720000000000) -> dict:
    """A SHAPE-valid, genuinely-signed reaction origin envelope. The gateway carries
    (doesn't verify), so shape-validity is what matters — but we sign the real
    reaction bytes so the envelope is authentic end-to-end."""
    raw_pub = priv.public_key().public_bytes_raw()
    sig = priv.sign(signing.reaction_signing_bytes(
        raw_pubkey=raw_pub, channel_id=channel_id, client_msg_id=client_msg_id,
        signed_at_ms=signed_at_ms, target_msg_id=target_msg_id, emoji=emoji,
        action=action))
    return {
        "v": 1, "alg": "EdDSA", "key_version": 1,
        "sender_pubkey": signing.encode_multikey(raw_pub),
        "client_msg_id": client_msg_id,
        "signed_at_ms": signed_at_ms,
        "sig": _b64url(sig),
    }


# ============================ SERVICE LAYER ================================

async def test_add_is_idempotent(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    changed1 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    changed2 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    assert changed1 is True
    assert changed2 is False  # re-add is a no-op, not a second row
    assert await reactions_service.emoji_count(
        session, msg.id, THUMB, blocked_user_ids=NO_BLOCK) == 1


async def test_resend_keeps_first_origin(session):
    """A re-add with a DIFFERENT signature keeps the FIRST row's origin (first
    signature wins: the signed path is ON CONFLICT DO UPDATE ... WHERE origin IS NULL,
    so a second signed add hits a non-NULL row and no-ops) — the idempotency key
    already pins the endorsement."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    o1 = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-1",
                        target_msg_id=msg.id, emoji=THUMB, signed_at_ms=1)
    o2 = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-1",
                        target_msg_id=msg.id, emoji=THUMB, signed_at_ms=999)

    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB, origin=o1)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB, origin=o2)
    row = await session.get(MessageReaction, (msg.id, reactor.id, THUMB))
    assert row.origin == o1  # first signature wins; o2 discarded


async def test_unsigned_row_upgrades_to_signed(session):
    """An UNSIGNED reaction that raced ahead must be upgradable to signed — an unsigned
    squatter can't permanently lock out the endorsement (cage-match Tesla r2 P1). But a
    second signed add never overwrites the first signature."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    signed = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-1",
                            target_msg_id=msg.id, emoji=THUMB)

    # unsigned first
    ch1 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB, origin=None)
    assert ch1 is True
    assert (await session.get(MessageReaction, (msg.id, reactor.id, THUMB))).origin is None
    # signed second → UPGRADES the row (but it's not a NEW reaction → changed False)
    ch2 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB, origin=signed)
    assert ch2 is False
    assert (await session.get(MessageReaction, (msg.id, reactor.id, THUMB))).origin == signed
    # a THIRD signed add with a different sig must NOT overwrite the first signature
    priv2 = Ed25519PrivateKey.generate()
    other = _signed_origin(priv2, channel_id=ch.id, client_msg_id="rxn-1",
                           target_msg_id=msg.id, emoji=THUMB, signed_at_ms=999)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB, origin=other)
    assert (await session.get(MessageReaction, (msg.id, reactor.id, THUMB))).origin == signed


async def test_blocked_pair_is_symmetric(session):
    """The reaction block-dual (history/mutate filter by VIEWER set; fanout excludes by
    ACTOR∪AUTHOR set) is a correct single predicate ONLY if blocked_pair_user_ids is
    symmetric (cage-match Tesla r2 P4). Pin that invariant so a future moderation change
    can't silently reopen the oracle at the WS/history seam."""
    a = await _user(session, "aaa")
    b = await _user(session, "bbb")
    await moderation_service.block_user(session, a.id, b.id)  # a blocks b (one direction)
    assert b.id in await moderation_service.blocked_pair_user_ids(session, a.id)
    assert a.id in await moderation_service.blocked_pair_user_ids(session, b.id)


async def test_aggregate_exposes_reactor_identity_and_origin(session):
    """Bounded-A: the aggregate carries reactors[] with user_id + the signed origin,
    NOT an anonymous count."""
    author = await _user(session, "author")
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    origin = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-a",
                            target_msg_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=alice.id, message_id=msg.id, emoji=THUMB, origin=origin)
    await reactions_service.add_reaction(  # bob unsigned
        session, user_id=bob.id, message_id=msg.id, emoji=THUMB)

    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=author.id, blocked_user_ids=NO_BLOCK)
    entry = agg[msg.id][0]
    assert entry["emoji"] == THUMB
    assert entry["count"] == 2
    assert entry["reacted_by_me"] is False
    reactors = {r["user_id"]: r for r in entry["reactors"]}
    assert reactors[alice.id]["origin"] == origin   # signed → origin echoed
    assert "origin" not in reactors[bob.id]          # unsigned → key omitted


async def test_reacted_by_me_is_viewer_dependent(session):
    author = await _user(session, "author")
    alice = await _user(session, "alice")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=alice.id, message_id=msg.id, emoji=THUMB)

    for viewer, expected in [(alice, True), (author, False)]:
        agg = await reactions_service.aggregate_for_messages(
            session, [msg.id], viewer_id=viewer.id, blocked_user_ids=NO_BLOCK)
        assert agg[msg.id][0]["reacted_by_me"] is expected


async def test_block_drops_reactor_from_identity_AND_count(session):
    """The crux. A blocked reactor is dropped from BOTH reactors[] and count — the
    same predicate on the whole projection, so there is no count oracle (the anonymous
    model's hazard where a filtered count disagreed with a global one)."""
    author = await _user(session, "author")
    viewer = await _user(session, "viewer")
    blocked = await _user(session, "blocked")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=viewer.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=blocked.id, message_id=msg.id, emoji=THUMB)

    # No block → viewer sees both.
    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=viewer.id, blocked_user_ids=NO_BLOCK)
    assert agg[msg.id][0]["count"] == 2

    # viewer blocks `blocked` → the reaction vanishes from identity AND count.
    block_set = {blocked.id}
    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=viewer.id, blocked_user_ids=block_set)
    entry = agg[msg.id][0]
    assert entry["count"] == 1                                   # count filtered too
    assert [r["user_id"] for r in entry["reactors"]] == [viewer.id]
    # The mutate-response count uses the SAME filtered tally — no oracle.
    assert await reactions_service.emoji_count(
        session, msg.id, THUMB, blocked_user_ids=block_set) == 1


async def test_emoji_group_fully_blocked_disappears(session):
    """If every reactor of an emoji is blocked, that emoji group is absent for the
    viewer (not a count-0 ghost)."""
    author = await _user(session, "author")
    viewer = await _user(session, "viewer")
    blocked = await _user(session, "blocked")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=blocked.id, message_id=msg.id, emoji=HEART)

    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=viewer.id, blocked_user_ids={blocked.id})
    assert msg.id not in agg  # no visible reactions at all


async def test_reactor_sample_bounded_but_count_authoritative(session):
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    n = reactions_service.MAX_REACTORS_PROJECTED_PER_EMOJI + 5
    for i in range(n):
        u = await _user(session, f"u{i}")
        await reactions_service.add_reaction(
            session, user_id=u.id, message_id=msg.id, emoji=THUMB)

    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=author.id, blocked_user_ids=NO_BLOCK)
    entry = agg[msg.id][0]
    assert entry["count"] == n  # authoritative full tally
    assert len(entry["reactors"]) == reactions_service.MAX_REACTORS_PROJECTED_PER_EMOJI


async def test_viewer_face_pinned_when_past_sample_window(session):
    """Tesla's triad (flag · count · sample): when the viewer reacted but the windowed
    sample (ordered by created_at) pushed their face past N, the viewer is still pinned
    into reactors[] so a faces-only client renders 'me' — count stays authoritative."""
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    # N earlier reactors, THEN the viewer last → viewer is row N+1 in created order.
    for i in range(reactions_service.MAX_REACTORS_PROJECTED_PER_EMOJI):
        u = await _user(session, f"early{i}")
        await reactions_service.add_reaction(
            session, user_id=u.id, message_id=msg.id, emoji=THUMB)
    viewer = await _user(session, "viewer")
    await reactions_service.add_reaction(
        session, user_id=viewer.id, message_id=msg.id, emoji=THUMB)

    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=viewer.id, blocked_user_ids=NO_BLOCK)
    entry = agg[msg.id][0]
    assert entry["count"] == reactions_service.MAX_REACTORS_PROJECTED_PER_EMOJI + 1
    assert entry["reacted_by_me"] is True
    assert viewer.id in {r["user_id"] for r in entry["reactors"]}  # pinned in
    # Pinning must NOT breach the advertised bound — replace, don't append.
    assert len(entry["reactors"]) <= reactions_service.MAX_REACTORS_PROJECTED_PER_EMOJI


async def test_own_emoji_group_survives_projection_truncation(session):
    """A rare emoji the viewer reacted with is kept even past the per-message
    projection cap — else reacted_by_me would vanish on re-page (self-heal
    corruption)."""
    author = await _user(session, "author")
    viewer = await _user(session, "viewer")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    cap = reactions_service.MAX_EMOJIS_PROJECTED_PER_MESSAGE
    # cap+1 popular emojis from many users, plus the viewer's own rare one.
    for e in range(cap + 1):
        for k in range(2):
            u = await _user(session, f"pop{e}_{k}")
            await reactions_service.add_reaction(
                session, user_id=u.id, message_id=msg.id, emoji=f"e{e:03d}")
    await reactions_service.add_reaction(
        session, user_id=viewer.id, message_id=msg.id, emoji="rare-💎")

    agg = await reactions_service.aggregate_for_messages(
        session, [msg.id], viewer_id=viewer.id, blocked_user_ids=NO_BLOCK)
    mine = [e for e in agg[msg.id] if e["emoji"] == "rare-💎"]
    assert mine and mine[0]["reacted_by_me"] is True


@pytest.mark.parametrize("bad", ["", " ", "  👍 ", "a/b", "x\x00y", "z" * 65])
async def test_validate_emoji_rejects_hazards(session, bad):
    with pytest.raises(reactions_service.InvalidEmoji):
        reactions_service.validate_emoji(bad)


async def test_per_user_distinct_emoji_cap(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    for i in range(reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE):
        await reactions_service.add_reaction(
            session, user_id=reactor.id, message_id=msg.id, emoji=f"e{i:03d}")
    with pytest.raises(reactions_service.ReactionLimitExceeded):
        await reactions_service.add_reaction(
            session, user_id=reactor.id, message_id=msg.id, emoji="one-too-many")
    # but re-adding an EXISTING emoji at the cap is still fine (idempotent no-op).
    assert await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji="e000") is False


async def test_purge_user_reactions(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.purge_user_reactions(session, reactor.id)
    await session.commit()
    assert await reactions_service.emoji_count(
        session, msg.id, THUMB, blocked_user_ids=NO_BLOCK) == 0


# ============================ ROUTE LAYER =================================

class _RecordingConn(Connection):
    """A Connection that records frames instead of touching a socket."""

    def __init__(self, user_id: str):
        self.ws = None  # type: ignore[assignment]
        self.user_id = user_id
        self.subscribed: set[str] = set()
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


@pytest_asyncio.fixture
async def app_ctx(session):
    """Minimal app: reaction + message routers, a real Hub on app.state.gw, the DB
    override. Returns (client, hub)."""
    hub = Hub()

    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(reaction_routes.router)
    app.include_router(message_routes.router)
    register_error_handlers(app)
    app.state.gw = SimpleNamespace(hub=hub)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, hub
    app.dependency_overrides.clear()


async def test_post_requires_auth(app_ctx):
    client, _ = app_ctx
    resp = await client.post(f"/v1/messages/{_ulid(1)}/reactions",
                             json={"emoji": THUMB})
    assert resp.status_code == 401


async def test_add_fans_out_identity_delta_no_count(app_ctx, session):
    """The live frame names the reactor + carries their origin, and carries NO count
    (an identity delta the client applies as a set change)."""
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    origin = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-1",
                            target_msg_id=msg.id, emoji=THUMB)

    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.post(
        f"/v1/messages/{msg.id}/reactions",
        json={"emoji": THUMB, "client_msg_id": "rxn-1", "origin": origin},
        headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json() == {"msg_id": msg.id, "emoji": THUMB, "count": 1,
                           "reacted_by_me": True}
    assert len(watcher.sent) == 1
    frame = watcher.sent[0]
    assert frame["type"] == "reaction"
    assert frame["action"] == ReactionAction.ADD
    assert frame["user_id"] == reactor.id
    assert frame["origin"] == origin
    # The frame carries the PERSISTED origin (durable truth history recomputes), not
    # merely the echoed request — they coincide here (no race) but the frame is read
    # back from the row (cage-match Tesla r3).
    persisted = await session.get(MessageReaction, (msg.id, reactor.id, THUMB))
    assert frame["origin"] == persisted.origin
    assert "count" not in frame  # identity delta, not a broadcast count


async def test_signed_origin_persisted_and_echoed_on_history(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    session.add(Membership(channel_id=ch.id, user_id=author.id, role="member"))
    await session.commit()
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    origin = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-1",
                            target_msg_id=msg.id, emoji=THUMB)

    await client.post(
        f"/v1/messages/{msg.id}/reactions",
        json={"emoji": THUMB, "client_msg_id": "rxn-1", "origin": origin},
        headers=_auth(reactor))

    resp = await client.get(f"/v1/channels/{ch.id}/messages", headers=_auth(author))
    assert resp.status_code == 200
    reactions = resp.json()["messages"][0]["reactions"]
    assert reactions[0]["emoji"] == THUMB
    assert reactions[0]["count"] == 1
    assert reactions[0]["reactors"][0]["user_id"] == reactor.id
    assert reactions[0]["reactors"][0]["origin"] == origin


async def test_unsigned_reaction_is_carried(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    row = await session.get(MessageReaction, (msg.id, reactor.id, THUMB))
    assert row.origin is None


async def test_malformed_origin_is_422(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(
        f"/v1/messages/{msg.id}/reactions",
        json={"emoji": THUMB, "client_msg_id": "rxn-1",
              "origin": {"v": 1, "alg": "EdDSA"}},  # missing keys
        headers=_auth(reactor))
    assert resp.status_code == 422


async def test_origin_client_msg_id_must_bind(app_ctx, session):
    """origin.client_msg_id must equal the request's client_msg_id (envelope-vs-payload
    confusion defense) — a mismatch is a 422."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    origin = _signed_origin(priv, channel_id=ch.id, client_msg_id="rxn-SIGNED",
                            target_msg_id=msg.id, emoji=THUMB)
    resp = await client.post(
        f"/v1/messages/{msg.id}/reactions",
        json={"emoji": THUMB, "client_msg_id": "rxn-DIFFERENT", "origin": origin},
        headers=_auth(reactor))
    assert resp.status_code == 422


async def test_signed_origin_without_client_msg_id_is_422(app_ctx, session):
    """A signed origin with no outer client_msg_id to bind against is rejected — a
    degenerate empty id must never satisfy the binding (cage-match Carnot r2)."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    priv = Ed25519PrivateKey.generate()
    origin = _signed_origin(priv, channel_id=ch.id, client_msg_id="",
                            target_msg_id=msg.id, emoji=THUMB)
    resp = await client.post(
        f"/v1/messages/{msg.id}/reactions",
        json={"emoji": THUMB, "origin": origin},  # no client_msg_id
        headers=_auth(reactor))
    assert resp.status_code == 422


async def test_react_to_invisible_message_is_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author, deleted=True)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404


async def test_bad_emoji_is_422_before_visibility(app_ctx, session):
    """A malformed emoji is 422 even on an invisible message — the 422/404 split can't
    be used to probe existence."""
    client, _ = app_ctx
    reactor = await _user(session, "reactor")
    resp = await client.post(f"/v1/messages/{_ulid(999)}/reactions",
                             json={"emoji": "a/b"}, headers=_auth(reactor))
    assert resp.status_code == 422


async def test_remove_own_reaction_survives_takedown(app_ctx, session):
    """Un-reacting your own row is ownership-authorised — allowed even after the
    message is taken down (soft-deleted), and the response count is null (the caller
    has lost visibility → no post-revocation count oracle)."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    # take the message down
    msg.deleted_at = dt.datetime.now(dt.timezone.utc)
    await session.commit()

    resp = await client.delete(
        f"/v1/messages/{msg.id}/reactions",
        params={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json()["count"] is None  # no oracle for a message they can't see
    assert await session.get(MessageReaction, (msg.id, reactor.id, THUMB)) is None


async def test_remove_absent_reaction_on_invisible_message_is_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    prober = await _user(session, "prober")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author, deleted=True)
    resp = await client.delete(
        f"/v1/messages/{msg.id}/reactions",
        params={"emoji": THUMB}, headers=_auth(prober))
    assert resp.status_code == 404  # can't distinguish "no row" from "can't see it"


async def test_history_read_is_block_consistent(app_ctx, session):
    """The history reactions[] applies the viewer's block predicate: a blocked
    reactor is absent from identity AND count — same as the service aggregate."""
    client, _ = app_ctx
    author = await _user(session, "author")
    viewer = await _user(session, "viewer")
    blocked = await _user(session, "blocked")
    ch = await _channel(session)
    session.add(Membership(channel_id=ch.id, user_id=viewer.id, role="member"))
    await session.commit()
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=viewer.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=blocked.id, message_id=msg.id, emoji=THUMB)
    await moderation_service.block_user(session, viewer.id, blocked.id)
    await moderation_service.block_user(session, blocked.id, viewer.id)

    resp = await client.get(f"/v1/channels/{ch.id}/messages", headers=_auth(viewer))
    reactions = resp.json()["messages"][0]["reactions"]
    assert reactions[0]["count"] == 1
    assert [r["user_id"] for r in reactions[0]["reactors"]] == [viewer.id]


async def test_fanout_excludes_block_pairs(app_ctx, session):
    """A subscriber in a block relationship with the reactor never receives the
    identity frame — that's what keeps a hidden reactor hidden (no count on the frame
    to filter). The reactor reacts to a NEUTRAL author's message (so the visibility
    gate passes); a third subscriber who blocks the reactor is the one excluded."""
    client, hub = app_ctx
    author = await _user(session, "author")     # neutral message author
    reactor = await _user(session, "reactor")
    blocker = await _user(session, "blocker")   # subscribed, blocks the reactor
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await moderation_service.block_user(session, blocker.id, reactor.id)
    await moderation_service.block_user(session, reactor.id, blocker.id)

    seen = _RecordingConn(author.id)      # no block → receives the frame
    seen.subscribed.add(ch.id)
    hub.register(seen)
    hidden = _RecordingConn(blocker.id)   # blocks reactor → excluded
    hidden.subscribed.add(ch.id)
    hub.register(hidden)

    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert len(seen.sent) == 1            # neutral subscriber sees it
    assert hidden.sent == []              # blocker never sees the reactor's frame


async def test_channel_hard_delete_purges_reactions(session):
    """verify-the-neighbor: hard-deleting a channel tears down its messages'
    reactions before the messages (they FK messages.id)."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session, name="doomed")
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    await channels_service.hard_delete_channel(session, "doomed")
    await session.commit()
    assert await reactions_service.emoji_count(
        session, msg.id, THUMB, blocked_user_ids=NO_BLOCK) == 0


async def test_cap_over_limit_is_429(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    for i in range(reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE):
        r = await client.post(f"/v1/messages/{msg.id}/reactions",
                              json={"emoji": f"e{i:03d}"}, headers=_auth(reactor))
        assert r.status_code == 200
    over = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": "one-too-many"}, headers=_auth(reactor))
    assert over.status_code == 429
