"""Membership-management endpoints (#46) — the WRITE side of the I2 boundary.

Companion to test_membership_acl.py (the READ side). These assert the trust
boundary on membership *mutations*: who may create channels, add/remove members,
self-join, and leave — and that the server REJECTS every bypass. The app-under-
test is built from JUST the membership router (never `main`) to keep the suite's
"never import aiko_services" isolation invariant.

Bypass-case matrix (each has a dedicated test asserting the server rejects it):
  (a) non-admin adding/removing others      -> 403
  (b) self-join to an invite_only channel   -> 404 (existence-hiding)
  (c) non-member listing private members    -> 404 (existence-hiding)
  (d) removing/leaving as the last admin    -> 409 (never orphan a channel)
  (e) double-join / double-add idempotency  -> 201, single row
  (f) leaving / removing a non-member       -> 404
"""
from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from aiko_gateway.domain import memberships_service as svc
from aiko_gateway.domain import security, users_service
from aiko_gateway.domain.models import Channel, Membership
from aiko_gateway.rest import members as member_routes
from aiko_gateway.rest.deps import get_session


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _channel(session, *, cid: int, name: str, is_private: bool,
                   join_policy: str = svc.JOIN_INVITE_ONLY) -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name,
                 is_private=is_private, join_policy=join_policy)
    session.add(ch)
    await session.commit()
    return ch


async def _member(session, channel, user, *, role: str = svc.ROLE_MEMBER,
                  can_post: bool = True) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, role=role,
                           can_post=can_post))
    await session.commit()


async def _count_members(session, channel_id: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(Membership)
        .where(Membership.channel_id == channel_id)
    )).scalar_one()


# =========================================================================
# Service layer — the single source of truth the REST routes delegate to
# =========================================================================

async def test_create_channel_makes_creator_an_admin(session):
    creator = await _user(session, "creator")
    ch = await svc.create_channel(
        session, creator_id=creator.id, name="club", is_private=True)
    m = (await session.execute(select(Membership).where(
        Membership.channel_id == ch.id, Membership.user_id == creator.id))).scalar_one()
    assert m.role == svc.ROLE_ADMIN
    assert ch.is_private and ch.join_policy == svc.JOIN_INVITE_ONLY


async def test_create_public_channel_forces_open_policy(session):
    creator = await _user(session, "creator")
    ch = await svc.create_channel(
        session, creator_id=creator.id, name="general", is_private=False,
        join_policy=svc.JOIN_INVITE_ONLY)
    # A public channel can't be invite_only — privacy gates membership, not policy.
    assert ch.join_policy == svc.JOIN_OPEN


async def test_add_member_is_idempotent(session):
    """(e) double-add returns the existing row, no duplicate, no role flip."""
    admin = await _user(session, "admin")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role=svc.ROLE_ADMIN)
    first = await svc.add_member(
        session, channel_id=ch.id, actor_id=admin.id, target_user_id=target.id,
        role=svc.ROLE_MEMBER, can_post=True)
    # Re-add with DIFFERENT params: must NOT flip the existing row.
    second = await svc.add_member(
        session, channel_id=ch.id, actor_id=admin.id, target_user_id=target.id,
        role=svc.ROLE_ADMIN, can_post=False)
    assert second.role == first.role == svc.ROLE_MEMBER
    assert await _count_members(session, ch.id) == 2  # admin + target, no dupe


async def test_self_join_open_private_channel_succeeds(session):
    ch = await _channel(session, cid=11, name="open-club", is_private=True,
                        join_policy=svc.JOIN_OPEN)
    bob = await _user(session, "bob")
    m = await svc.self_join(session, channel_id=ch.id, actor_id=bob.id)
    assert m.user_id == bob.id and m.role == svc.ROLE_MEMBER


async def test_self_join_idempotent(session):
    """(e) self-joining twice returns the same row, no duplicate."""
    ch = await _channel(session, cid=11, name="open-club", is_private=True,
                        join_policy=svc.JOIN_OPEN)
    bob = await _user(session, "bob")
    await svc.self_join(session, channel_id=ch.id, actor_id=bob.id)
    await svc.self_join(session, channel_id=ch.id, actor_id=bob.id)
    assert await _count_members(session, ch.id) == 1


# =========================================================================
# REST seam — bypass-case matrix (the server MUST reject each)
# =========================================================================

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(member_routes.router)
    return app


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = _build_app()
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


# --- happy paths -----------------------------------------------------------

async def test_create_channel_endpoint_auto_admin(client, session):
    creator = await _user(session, "creator")
    resp = await client.post("/v1/channels", headers=await _headers(creator),
                             json={"name": "vault", "is_private": True})
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_private"] is True and body["join_policy"] == "invite_only"
    # creator is an admin member
    m = (await session.execute(select(Membership).where(
        Membership.channel_id == body["id"]))).scalar_one()
    assert m.user_id == creator.id and m.role == "admin"


async def test_admin_adds_member(client, session):
    admin = await _user(session, "admin")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.post(f"/v1/channels/{ch.id}/members",
                             headers=await _headers(admin),
                             json={"user_id": target.id})
    assert resp.status_code == 201
    assert resp.json()["user_id"] == target.id


async def test_admin_removes_member(client, session):
    admin = await _user(session, "admin")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    await _member(session, ch, target)
    resp = await client.request("DELETE",
                                f"/v1/channels/{ch.id}/members/{target.id}",
                                headers=await _headers(admin))
    assert resp.status_code == 204
    assert await _count_members(session, ch.id) == 1


async def test_member_leaves(client, session):
    admin = await _user(session, "admin")
    bob = await _user(session, "bob")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    await _member(session, ch, bob)
    resp = await client.request("DELETE", f"/v1/channels/{ch.id}/leave",
                                headers=await _headers(bob))
    assert resp.status_code == 204
    assert await _count_members(session, ch.id) == 1


async def test_self_join_open_channel_via_endpoint(client, session):
    ch = await _channel(session, cid=11, name="open-club", is_private=True,
                        join_policy="open")
    bob = await _user(session, "bob")
    resp = await client.post(f"/v1/channels/{ch.id}/join",
                             headers=await _headers(bob))
    assert resp.status_code == 201
    assert resp.json()["user_id"] == bob.id


async def test_member_can_list_members(client, session):
    admin = await _user(session, "admin")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.get(f"/v1/channels/{ch.id}/members",
                            headers=await _headers(admin))
    assert resp.status_code == 200
    assert [m["user_id"] for m in resp.json()["members"]] == [admin.id]


# --- bypass case (a): non-admin add/remove --------------------------------

async def test_non_admin_cannot_add_member(client, session):
    member = await _user(session, "member")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, member, role="member")  # NOT admin
    resp = await client.post(f"/v1/channels/{ch.id}/members",
                             headers=await _headers(member),
                             json={"user_id": target.id})
    assert resp.status_code == 403
    assert await _count_members(session, ch.id) == 1  # nothing added


async def test_non_admin_cannot_remove_member(client, session):
    member = await _user(session, "member")
    other = await _user(session, "other")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, member, role="member")
    await _member(session, ch, other)
    resp = await client.request("DELETE",
                                f"/v1/channels/{ch.id}/members/{other.id}",
                                headers=await _headers(member))
    assert resp.status_code == 403
    assert await _count_members(session, ch.id) == 2  # nothing removed


# --- bypass case (b): self-join to invite_only (existence-hiding) ---------

async def test_self_join_invite_only_private_is_404(client, session):
    """A non-member must not distinguish 'invite_only' from 'nonexistent'."""
    ch = await _channel(session, cid=10, name="secret", is_private=True,
                        join_policy="invite_only")
    bob = await _user(session, "bob")
    resp = await client.post(f"/v1/channels/{ch.id}/join",
                             headers=await _headers(bob))
    assert resp.status_code == 404
    assert await _count_members(session, ch.id) == 0  # not joined


async def test_self_join_nonexistent_channel_is_404(client, session):
    """Same 404 as invite_only — the two are indistinguishable by design."""
    bob = await _user(session, "bob")
    resp = await client.post("/v1/channels/00000000000000000000099999/join",
                             headers=await _headers(bob))
    assert resp.status_code == 404


# --- bypass case (c): non-member listing private members ------------------

async def test_non_member_cannot_list_private_members_404(client, session):
    admin = await _user(session, "admin")
    outsider = await _user(session, "outsider")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.get(f"/v1/channels/{ch.id}/members",
                            headers=await _headers(outsider))
    # Existence-hiding: identical to a nonexistent channel.
    assert resp.status_code == 404


async def test_list_members_nonexistent_channel_404(client, session):
    bob = await _user(session, "bob")
    resp = await client.get("/v1/channels/00000000000000000000099999/members",
                            headers=await _headers(bob))
    assert resp.status_code == 404


# --- bypass case (d): removing/leaving the last admin ---------------------

async def test_cannot_remove_last_admin(client, session):
    admin = await _user(session, "admin")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.request("DELETE",
                                f"/v1/channels/{ch.id}/members/{admin.id}",
                                headers=await _headers(admin))
    assert resp.status_code == 409
    assert await _count_members(session, ch.id) == 1  # still there


async def test_cannot_leave_as_last_admin(client, session):
    admin = await _user(session, "admin")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.request("DELETE", f"/v1/channels/{ch.id}/leave",
                                headers=await _headers(admin))
    assert resp.status_code == 409


async def test_admin_can_leave_when_another_admin_remains(client, session):
    a1 = await _user(session, "admin1")
    a2 = await _user(session, "admin2")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, a1, role="admin")
    await _member(session, ch, a2, role="admin")
    resp = await client.request("DELETE", f"/v1/channels/{ch.id}/leave",
                                headers=await _headers(a1))
    assert resp.status_code == 204
    assert await _count_members(session, ch.id) == 1


# --- bypass case (e): double-join idempotency (endpoint) ------------------

async def test_double_join_is_idempotent_endpoint(client, session):
    ch = await _channel(session, cid=11, name="open-club", is_private=True,
                        join_policy="open")
    bob = await _user(session, "bob")
    r1 = await client.post(f"/v1/channels/{ch.id}/join", headers=await _headers(bob))
    r2 = await client.post(f"/v1/channels/{ch.id}/join", headers=await _headers(bob))
    assert r1.status_code == 201 and r2.status_code == 201
    assert await _count_members(session, ch.id) == 1


async def test_double_add_is_idempotent_endpoint(client, session):
    admin = await _user(session, "admin")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    body = {"user_id": target.id}
    await client.post(f"/v1/channels/{ch.id}/members", headers=await _headers(admin), json=body)
    await client.post(f"/v1/channels/{ch.id}/members", headers=await _headers(admin), json=body)
    assert await _count_members(session, ch.id) == 2  # admin + target only


# --- bypass case (f): leaving / removing a non-member ---------------------

async def test_leave_channel_youre_not_in_is_404(client, session):
    admin = await _user(session, "admin")
    bob = await _user(session, "bob")  # never joined
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.request("DELETE", f"/v1/channels/{ch.id}/leave",
                                headers=await _headers(bob))
    assert resp.status_code == 404


async def test_remove_non_member_is_404(client, session):
    admin = await _user(session, "admin")
    stranger = await _user(session, "stranger")  # not a member
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.request("DELETE",
                                f"/v1/channels/{ch.id}/members/{stranger.id}",
                                headers=await _headers(admin))
    assert resp.status_code == 404


# =========================================================================
# Cage-match PR#10 hardening — fixes for the adversarial findings
# =========================================================================

async def test_add_nonexistent_user_is_controlled_404_not_500(client, session):
    """Carnot, PR#10: adding a user that doesn't exist must be a controlled
    rejection (NotAMember -> 404), NOT an FK IntegrityError surfacing as a 500
    at commit time."""
    admin = await _user(session, "admin")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    resp = await client.post(f"/v1/channels/{ch.id}/members",
                             headers=await _headers(admin),
                             json={"user_id": "00000000000000000000nouser"})
    assert resp.status_code == 404
    assert await _count_members(session, ch.id) == 1  # nothing added


async def test_idempotent_insert_recovers_from_integrity_error(session):
    """Carnot, PR#10: the composite-PK constraint is the real idempotency
    authority. Simulate the race loser: a row already exists, a second insert of
    the same (channel,user) hits the unique constraint — _insert_idempotent must
    catch it, roll back, and return the existing row rather than raising 500."""
    from aiko_gateway.domain.models import Membership
    admin = await _user(session, "admin")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    await _member(session, ch, admin, role="admin")
    await _member(session, ch, target)  # winner's row already committed
    # The "loser" payload: same PK, different attrs. Must NOT raise; returns the
    # already-present row (idempotent).
    dup = Membership(channel_id=ch.id, user_id=target.id, role="admin",
                     can_post=False)
    got = await svc._insert_idempotent(session, dup, ch.id, target.id)
    assert got.user_id == target.id and got.role == "member"  # existing, not the dup
    assert await _count_members(session, ch.id) == 2


async def test_self_join_resolver_single_query_no_pk_lookup(session):
    """Carnot, PR#10: self_join must resolve existence+joinability in ONE query
    (no bare PK lookup that distinguishes invite_only from nonexistent by query
    shape). Functional proof: invite_only-private and nonexistent both raise
    ChannelNotFound; open-private and public both succeed for a non-member."""
    bob = await _user(session, "bob")
    invite_only = await _channel(session, cid=10, name="io", is_private=True,
                                 join_policy="invite_only")
    open_priv = await _channel(session, cid=11, name="op", is_private=True,
                               join_policy="open")
    public = await _channel(session, cid=12, name="pub", is_private=False)

    import pytest
    with pytest.raises(svc.ChannelNotFound):
        await svc.self_join(session, channel_id=invite_only.id, actor_id=bob.id)
    with pytest.raises(svc.ChannelNotFound):
        await svc.self_join(session, channel_id="00000000000000000000nochan",
                            actor_id=bob.id)
    # Open-private and public both joinable by a non-member.
    assert (await svc.self_join(session, channel_id=open_priv.id,
                                actor_id=bob.id)).user_id == bob.id
    assert (await svc.self_join(session, channel_id=public.id,
                                actor_id=bob.id)).user_id == bob.id


async def test_existing_member_of_invite_only_can_rejoin_idempotently(session):
    """A member of an invite_only private channel self-joining again must NOT be
    404'd — the single-query resolver includes the 'already a member' branch, so
    an existing member resolves the channel and gets an idempotent no-op."""
    alice = await _user(session, "alice")
    ch = await _channel(session, cid=10, name="io", is_private=True,
                        join_policy="invite_only")
    await _member(session, ch, alice)
    got = await svc.self_join(session, channel_id=ch.id, actor_id=alice.id)
    assert got.user_id == alice.id
    assert await _count_members(session, ch.id) == 1


async def test_invalid_join_policy_rejected_at_api_boundary(client, session):
    """Carnot, PR#10: join_policy is a JoinPolicy enum on the request model, so a
    bogus value is a 422 at the boundary, not a silent coercion to invite_only."""
    creator = await _user(session, "creator")
    resp = await client.post("/v1/channels", headers=await _headers(creator),
                             json={"name": "x", "is_private": True,
                                   "join_policy": "everyone-welcome"})
    assert resp.status_code == 422


async def test_created_channel_aiko_channel_derived_from_id(session):
    """Kelvin, PR#10: aiko_channel must be derived from the channel id (one ULID),
    not a second independent ULID — so the wire name maps 1:1 to the row."""
    creator = await _user(session, "creator")
    ch = await svc.create_channel(session, creator_id=creator.id, name="x",
                                  is_private=True)
    assert ch.aiko_channel == f"ch_{ch.id}"


# --- existence-hiding: admin ops on an unseeable channel are 404, not 403 --

async def test_add_member_to_unseeable_private_channel_is_404(client, session):
    """An outsider (non-member) trying to add to a private channel must get the
    SAME 404 as a nonexistent channel — not 403, which would confirm it exists."""
    outsider = await _user(session, "outsider")
    target = await _user(session, "target")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    resp = await client.post(f"/v1/channels/{ch.id}/members",
                             headers=await _headers(outsider),
                             json={"user_id": target.id})
    assert resp.status_code == 404


async def test_remove_member_from_unseeable_private_channel_is_404(client, session):
    outsider = await _user(session, "outsider")
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    member = await _user(session, "member")
    await _member(session, ch, member)
    resp = await client.request("DELETE",
                                f"/v1/channels/{ch.id}/members/{member.id}",
                                headers=await _headers(outsider))
    assert resp.status_code == 404


# --- auth: every route rejects an unauthenticated caller (I1) -------------

async def test_routes_require_auth(client, session):
    ch = await _channel(session, cid=10, name="secret", is_private=True)
    for method, path, kw in [
        ("POST", "/v1/channels", {"json": {"name": "x"}}),
        ("GET", f"/v1/channels/{ch.id}/members", {}),
        ("POST", f"/v1/channels/{ch.id}/members", {"json": {"user_id": "u"}}),
        ("DELETE", f"/v1/channels/{ch.id}/members/u", {}),
        ("POST", f"/v1/channels/{ch.id}/join", {}),
        ("DELETE", f"/v1/channels/{ch.id}/leave", {}),
    ]:
        resp = await client.request(method, path, **kw)
        assert resp.status_code in (401, 403), f"{method} {path} not auth-gated"
