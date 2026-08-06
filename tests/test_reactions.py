"""Acceptance tests for emoji reactions (#2634, v2 social layer).

Two layers, mirroring the codebase's split:
  * SERVICE — idempotency, viewer-dependent aggregation, validation, the
    account-deletion purge, folded through the ONE door (reactions_service).
  * ROUTE — auth, the existence-hiding visibility gate (soft-delete + channel ACL
    + block), the discrete `reaction` WS fanout, and the viewer-dependent
    reactions[] on the history read.

Built from JUST the reaction + message routers (never `aiko_gateway.main`, which
imports the aiko bus — the suite's "never import aiko_services" isolation
invariant). The DB `get_session` dependency is overridden to the in-memory test
session so the endpoint and the auth dependency share one DB; a real `Hub` is
wired onto `app.state.gw` so the fanout is exercised, not mocked.

Contract under test (mirrors HANDOFF-from-app-tab-v2-social-wire, task #2634):
  - reaction is STATE not event — no forward-ULID row; the aggregate is recomputed
    on every history read (a live `reaction` frame is a best-effort delta over it)
  - `count` + `reacted_by_me` per emoji is the whole read shape (no reactors list)
  - add/remove idempotent; emoji opaque + length-bounded
  - you may only react to a message you can SEE (same predicate as history)
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import (
    accounts_service, messages_service, moderation_service, reactions_service,
    security, users_service)
from aiko_gateway.domain.models import Channel, Membership, Message
from aiko_gateway.realtime.hub import Connection, Hub
from aiko_gateway.rest import messages as message_routes
from aiko_gateway.rest import reactions as reaction_routes
from aiko_gateway.rest.deps import get_session
from aiko_gateway.rest.errors import register_error_handlers

THUMB = "\U0001f44d"   # 👍
HEART = "❤️"  # ❤️


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


# ============================ SERVICE LAYER ================================

async def test_add_is_idempotent(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    c1, changed1 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c2, changed2 = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    assert (c1, changed1) == (1, True)
    assert (c2, changed2) == (1, False)  # re-add is a no-op, not a second row


async def test_distinct_emoji_are_distinct_rows(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    heart, changed = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=HEART)
    assert (heart, changed) == (1, True)  # a different emoji is its own reaction


async def test_two_users_same_emoji_counts_two(session):
    author = await _user(session, "author")
    a = await _user(session, "aaa")
    b = await _user(session, "bbb")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=a.id, message_id=msg.id, emoji=THUMB)
    count, changed = await reactions_service.add_reaction(
        session, user_id=b.id, message_id=msg.id, emoji=THUMB)
    assert (count, changed) == (2, True)


async def test_remove_is_idempotent(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c1, changed1 = await reactions_service.remove_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    c2, changed2 = await reactions_service.remove_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)
    assert (c1, changed1) == (0, True)
    assert (c2, changed2) == (0, False)  # removing an absent reaction is a no-op


async def test_aggregate_is_viewer_dependent_and_ordered(session):
    author = await _user(session, "author")
    me = await _user(session, "me")
    other = await _user(session, "other")
    third = await _user(session, "third")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    # THUMB: me + other + third (count 3, mine). HEART: other only (count 1, not mine).
    for u in (me, other, third):
        await reactions_service.add_reaction(
            session, user_id=u.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=other.id, message_id=msg.id, emoji=HEART)

    agg = await reactions_service.aggregate_for_messages(session, [msg.id], me.id)
    entries = agg[msg.id]
    # Ordered most-reacted first (-count), so THUMB (3) precedes HEART (1).
    assert [e["emoji"] for e in entries] == [THUMB, HEART]
    assert entries[0] == {"emoji": THUMB, "count": 3, "reacted_by_me": True}
    assert entries[1] == {"emoji": HEART, "count": 1, "reacted_by_me": False}

    # Same rows, DIFFERENT viewer — reacted_by_me flips where appropriate.
    agg_other = await reactions_service.aggregate_for_messages(
        session, [msg.id], other.id)
    by_emoji = {e["emoji"]: e for e in agg_other[msg.id]}
    assert by_emoji[THUMB]["reacted_by_me"] is True
    assert by_emoji[HEART]["reacted_by_me"] is True


async def test_aggregate_absent_for_unreacted_message(session):
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}  # no entry → serializer falls back to []


async def test_aggregate_empty_ids_short_circuits(session):
    assert await reactions_service.aggregate_for_messages(session, [], "whoever") == {}


@pytest.mark.parametrize("bad", [
    "",                # empty
    "   ",             # blank
    "x" * 65,          # over length cap
    " \U0001f44d",     # leading whitespace (lookalike of the stripped form)
    "\U0001f44d ",     # trailing whitespace
    "a/b",             # path separator — un-deletable via /reactions/{emoji}
    "a\tb",            # control char (tab)
    "a\x00b",          # NUL
])
async def test_validate_emoji_rejects(bad):
    with pytest.raises(reactions_service.InvalidEmoji):
        reactions_service.validate_emoji(bad)


async def test_validate_emoji_accepts_zwj_sequence():
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # 👨‍👩‍👧
    assert reactions_service.validate_emoji(family) == family


async def test_add_reaction_validates_at_the_door(session):
    """The door validates even when the route didn't — an in-process caller can't
    write a malformed emoji straight to the PK (cage-match Tesla: one door with a
    real latch)."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    with pytest.raises(reactions_service.InvalidEmoji):
        await reactions_service.add_reaction(
            session, user_id=reactor.id, message_id=msg.id, emoji="a/b")


async def test_per_message_reaction_cap(session):
    """A user cannot spray more than MAX distinct emojis on one message (single-actor
    payload/DoS bound), but may still re-toggle emojis already placed."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    cap = reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE
    for i in range(cap):
        await reactions_service.add_reaction(
            session, user_id=reactor.id, message_id=msg.id, emoji=f"e{i}")
    # A NEW distinct emoji beyond the cap is refused...
    with pytest.raises(reactions_service.ReactionLimitExceeded):
        await reactions_service.add_reaction(
            session, user_id=reactor.id, message_id=msg.id, emoji="over")
    # ...but re-adding one already placed is still a no-op success, not a limit error.
    count, changed = await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji="e0")
    assert (count, changed) == (1, False)


async def test_aggregate_count_is_global_anonymous_even_with_block(session):
    """The count is a GLOBAL anonymous tally — NOT block-filtered (cage-match round 2).
    Block-filtering the count would create a count oracle (a viewer-dependent count
    disagreeing with the global WS-frame count leaks that a blocked user reacted). The
    v2 API exposes no reactor list, so the count reveals no identity; what a block
    hides is the identity-bearing live frame (covered by the fanout tests), not the
    anonymous number. So me sees count 2 whether or not me has blocked a reactor."""
    author = await _user(session, "author")
    me = await _user(session, "me")
    troll = await _user(session, "troll")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=me.id, message_id=msg.id, emoji=THUMB)
    await reactions_service.add_reaction(
        session, user_id=troll.id, message_id=msg.id, emoji=THUMB)

    await moderation_service.block_user(session, me.id, troll.id)
    agg = await reactions_service.aggregate_for_messages(session, [msg.id], me.id)
    # Count stays 2 (global) — the same number the WS frame + mutate response carry, so
    # no path disagrees and no oracle exists.
    assert agg[msg.id] == [{"emoji": THUMB, "count": 2, "reacted_by_me": True}]


async def test_message_view_defaults_reactions_empty(session):
    author = await _user(session, "author")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    view = messages_service.message_view(msg)
    assert view["reactions"] == []


async def test_purge_user_reactions(session):
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    await reactions_service.purge_user_reactions(session, reactor.id)
    await session.commit()
    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}


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


async def test_post_adds_and_fans_out(app_ctx, session):
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json() == {"msg_id": msg.id, "emoji": THUMB, "count": 1,
                           "reacted_by_me": True}
    frames = [f for f in watcher.sent if f.get("type") == "reaction"]
    assert len(frames) == 1
    assert frames[0] == {"type": "reaction", "channel_id": ch.id, "msg_id": msg.id,
                         "emoji": THUMB, "action": "add", "user_id": reactor.id,
                         "count": 1}


async def test_post_is_idempotent_over_http(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)

    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json()["count"] == 1  # still one


async def test_post_invalid_emoji_422(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": "   "}, headers=_auth(reactor))
    assert resp.status_code == 422


async def test_post_to_missing_message_404(app_ctx, session):
    client, _ = app_ctx
    reactor = await _user(session, "reactor")
    resp = await client.post(f"/v1/messages/{_ulid(999)}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404


async def test_post_to_soft_deleted_message_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author, deleted=True)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404  # can't react to a taken-down message


async def test_post_to_private_channel_non_member_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    outsider = await _user(session, "outsider")
    ch = await _channel(session, private=True)
    # author is a member (can post); outsider is not.
    session.add(Membership(channel_id=ch.id, user_id=author.id, role="member"))
    await session.commit()
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(outsider))
    assert resp.status_code == 404  # existence-hiding: same 404 as non-existent


async def test_post_to_blocked_authors_message_404(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    # reactor blocks author → author's message is hidden from reactor → un-reactable.
    await moderation_service.block_user(session, reactor.id, author.id)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 404


async def test_delete_removes_and_fans_out(app_ctx, session):
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.delete(
        f"/v1/messages/{msg.id}/reactions/{THUMB}", headers=_auth(reactor))
    assert resp.status_code == 200
    assert resp.json() == {"msg_id": msg.id, "emoji": THUMB, "count": 0,
                           "reacted_by_me": False}
    frames = [f for f in watcher.sent if f.get("type") == "reaction"]
    assert frames[0]["action"] == "remove"
    assert frames[0]["count"] == 0


async def test_fanout_excludes_reactor_block_pairs(app_ctx, session):
    """A subscriber the reactor has blocked does NOT receive the reaction frame — the
    live twin of the history visibility filter (mirrors the send path)."""
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await moderation_service.block_user(session, reactor.id, author.id)

    watcher = _RecordingConn(author.id)  # author is blocked by the reactor
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    # reactor reacts to their OWN-visible message (a third party's would 404); use a
    # self-authored message so the block gate on the resolve doesn't 404 the reactor.
    own = await _msg(session, mid=2, channel=ch, sender=reactor)
    resp = await client.post(f"/v1/messages/{own.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    # author (blocked by reactor) must NOT see the reactor's reaction frame.
    assert [f for f in watcher.sent if f.get("type") == "reaction"] == []


async def test_repost_same_emoji_fires_no_second_frame(app_ctx, session):
    """A re-POST of an emoji the user already placed is a no-op (changed=False) and
    must NOT rebroadcast — no free WS chatter (cage-match Tesla)."""
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))
    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))
    # Exactly ONE reaction frame for two POSTs — the second was a no-op.
    assert len([f for f in watcher.sent if f.get("type") == "reaction"]) == 1


async def test_delete_absent_reaction_fires_no_frame(app_ctx, session):
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    watcher = _RecordingConn(author.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.delete(
        f"/v1/messages/{msg.id}/reactions/{THUMB}", headers=_auth(reactor))
    assert resp.status_code == 200  # idempotent
    assert [f for f in watcher.sent if f.get("type") == "reaction"] == []


async def test_post_slash_emoji_422(app_ctx, session):
    """An emoji with a '/' is rejected — it would be un-removable via the DELETE path
    segment (cage-match Carnot)."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": "a/b"}, headers=_auth(reactor))
    assert resp.status_code == 422


async def test_post_over_cap_429(app_ctx, session):
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    cap = reactions_service.MAX_REACTIONS_PER_USER_PER_MESSAGE
    for i in range(cap):
        r = await client.post(f"/v1/messages/{msg.id}/reactions",
                              json={"emoji": f"e{i}"}, headers=_auth(reactor))
        assert r.status_code == 200
    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": "over"}, headers=_auth(reactor))
    assert resp.status_code == 429


async def test_fanout_excludes_message_author_block_pairs(app_ctx, session):
    """A subscriber who blocked the MESSAGE AUTHOR (but not the reactor) must NOT
    receive the reaction frame — they can't see the message, so the live path must
    hide its reactions too (the fanout twin of aggregate's block filter)."""
    client, hub = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    # watcher blocked the author → can't see the message → must not see its reactions.
    # (reactor is in NO block relationship, so it can still see + react to the message.)
    blocker = await _user(session, "blocker")
    await moderation_service.block_user(session, blocker.id, author.id)
    watcher = _RecordingConn(blocker.id)
    watcher.subscribed.add(ch.id)
    hub.register(watcher)

    resp = await client.post(f"/v1/messages/{msg.id}/reactions",
                             json={"emoji": THUMB}, headers=_auth(reactor))
    assert resp.status_code == 200
    assert [f for f in watcher.sent if f.get("type") == "reaction"] == []


async def test_history_reflects_reactions_viewer_dependently(app_ctx, session):
    """The history read carries the viewer-dependent reactions[] on each message —
    reacted_by_me is TRUE for the reactor, FALSE for another reader."""
    client, _ = app_ctx
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await client.post(f"/v1/messages/{msg.id}/reactions",
                      json={"emoji": THUMB}, headers=_auth(reactor))

    # The reactor sees reacted_by_me = True.
    resp = await client.get(
        f"/v1/channels/{ch.id}/messages", headers=_auth(reactor))
    item = next(m for m in resp.json()["messages"] if m["msg_id"] == msg.id)
    assert item["reactions"] == [{"emoji": THUMB, "count": 1, "reacted_by_me": True}]

    # Another reader sees the same count but reacted_by_me = False.
    resp2 = await client.get(
        f"/v1/channels/{ch.id}/messages", headers=_auth(author))
    item2 = next(m for m in resp2.json()["messages"] if m["msg_id"] == msg.id)
    assert item2["reactions"] == [{"emoji": THUMB, "count": 1, "reacted_by_me": False}]


async def test_account_deletion_purges_reactions(session):
    """A deleted user's reactions vanish (the cascade purge), and a message they
    reacted to loses that count — proven end-to-end through delete_user_account."""
    author = await _user(session, "author")
    reactor = await _user(session, "reactor")
    ch = await _channel(session)
    msg = await _msg(session, mid=1, channel=ch, sender=author)
    await reactions_service.add_reaction(
        session, user_id=reactor.id, message_id=msg.id, emoji=THUMB)

    await accounts_service.delete_user_account(session, reactor.id)

    agg = await reactions_service.aggregate_for_messages(session, [msg.id], author.id)
    assert agg == {}
