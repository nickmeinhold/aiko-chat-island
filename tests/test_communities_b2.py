"""ATDD — communities Phase B2: discover / detail / join / my-communities (#32).

The trust surface B1 was kept invisible to protect. These specs pin the visibility
predicate across the THREE new read paths (discover, detail, join) and the
fail-closed join gate (global lesson b2d9 — within-instant visibility consistency:
a community can flip public->private or be taken down BETWEEN a viewer's discover
listing and their join; join is the authoritative gate and must reject the stale
case).

Layout mirrors test_membership_management.py: service-layer specs (the single
enforcement source) then a REST seam (the wire contract + existence-hiding status
mapping). The app-under-test is built from JUST the communities router (never
`main`) to keep the suite's "never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from aiko_gateway.domain import communities_service as svc
from aiko_gateway.domain import security, users_service
from aiko_gateway.domain.models import (
    Channel, Community, CommunityMembership, Membership,
)
from aiko_gateway.rest import communities as community_routes
from aiko_gateway.rest.deps import get_session


def _ulid(n: int) -> str:
    return f"{n:026d}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _community(
    session, *, cid: int, name: str, visibility: str = "public",
    category: str = "general", member_count: int = 0, taken_down: bool = False,
) -> Community:
    c = Community(
        id=_ulid(cid), name=name, visibility=visibility, category=category,
        member_count=member_count,
        taken_down_at=_utcnow() if taken_down else None,
    )
    session.add(c)
    await session.commit()
    return c


async def _channel(
    session, *, cid: int, name: str, community_id: str, is_private: bool = False,
) -> Channel:
    ch = Channel(
        id=_ulid(cid), name=name, kind="standard", aiko_channel=f"aiko/{name}",
        is_private=is_private, community_id=community_id,
    )
    session.add(ch)
    await session.commit()
    return ch


async def _community_member(session, community, user) -> None:
    session.add(CommunityMembership(
        community_id=community.id, user_id=user.id, role="member"))
    await session.commit()


# =========================================================================
# Service layer — discover (the public directory)
# =========================================================================

async def test_discover_lists_only_public_not_taken_down(session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="Public", visibility="public")
    await _community(session, cid=2, name="Unlisted", visibility="unlisted")
    await _community(session, cid=3, name="Private", visibility="private")
    await _community(session, cid=4, name="Downed", visibility="public",
                     taken_down=True)
    rows, _ = await svc.discover(session, viewer_id=viewer.id)
    assert [c.name for c in rows] == ["Public"]


async def test_discover_q_filters_by_name_substring(session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="Python Devs")
    await _community(session, cid=2, name="Rust Devs")
    await _community(session, cid=3, name="Cooking")
    rows, _ = await svc.discover(session, viewer_id=viewer.id, q="devs")
    assert {c.name for c in rows} == {"Python Devs", "Rust Devs"}


async def test_discover_category_filters_exact(session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="A", category="tech")
    await _community(session, cid=2, name="B", category="gaming")
    rows, _ = await svc.discover(session, viewer_id=viewer.id, category="gaming")
    assert [c.name for c in rows] == ["B"]


async def test_discover_cursor_paginates_without_gaps_or_dupes(session):
    """Walk the whole directory via the cursor; every public community appears
    exactly once and pages don't overlap (keyset correctness)."""
    viewer = await _user(session, "viewer")
    total = svc.PAGE_SIZE * 2 + 3  # spans three pages
    for i in range(1, total + 1):
        await _community(session, cid=i, name=f"C{i:03d}",
                         member_count=i)  # distinct counts -> deterministic order
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        rows, cursor = await svc.discover(
            session, viewer_id=viewer.id, sort="members", cursor=cursor)
        seen.extend(c.id for c in rows)
        pages += 1
        assert len(rows) <= svc.PAGE_SIZE
        if cursor is None:
            break
        assert pages < 10  # guard against an infinite loop
    assert len(seen) == total
    assert len(set(seen)) == total  # no duplicates across pages


async def test_discover_sort_members_desc(session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="Small", member_count=5)
    await _community(session, cid=2, name="Big", member_count=50)
    await _community(session, cid=3, name="Mid", member_count=20)
    rows, _ = await svc.discover(session, viewer_id=viewer.id, sort="members")
    assert [c.name for c in rows] == ["Big", "Mid", "Small"]


async def test_discover_sort_name_asc(session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="Charlie")
    await _community(session, cid=2, name="Alpha")
    await _community(session, cid=3, name="Bravo")
    rows, _ = await svc.discover(session, viewer_id=viewer.id, sort="name")
    assert [c.name for c in rows] == ["Alpha", "Bravo", "Charlie"]


async def test_discover_invalid_cursor_raises(session):
    viewer = await _user(session, "viewer")
    with pytest.raises(svc.InvalidCursor):
        await svc.discover(session, viewer_id=viewer.id, cursor="!!!not-base64!!!")


# =========================================================================
# Service layer — detail (fail closed) + visible channels
# =========================================================================

async def test_detail_returns_community_and_visible_public_channels(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub")
    await _channel(session, cid=10, name="general", community_id=c.id)
    await _channel(session, cid=11, name="random", community_id=c.id)
    community, channels = await svc.community_detail(
        session, viewer_id=viewer.id, community_id=c.id)
    assert community.id == c.id
    assert {ch.name for ch in channels} == {"general", "random"}


async def test_detail_hides_private_channel_from_non_member(session):
    """A private channel in the community is existence-hidden from a non-member,
    reusing the SAME acl readable predicate as the flat channel list."""
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub")
    await _channel(session, cid=10, name="general", community_id=c.id)
    await _channel(session, cid=11, name="secret", community_id=c.id,
                   is_private=True)
    _, channels = await svc.community_detail(
        session, viewer_id=viewer.id, community_id=c.id)
    assert {ch.name for ch in channels} == {"general"}  # secret hidden


async def test_detail_private_community_non_member_fails_closed(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Cabal", visibility="private")
    with pytest.raises(svc.CommunityNotFound):
        await svc.community_detail(session, viewer_id=viewer.id, community_id=c.id)


async def test_detail_private_community_member_can_see(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Cabal", visibility="private")
    await _community_member(session, c, viewer)
    community, _ = await svc.community_detail(
        session, viewer_id=viewer.id, community_id=c.id)
    assert community.id == c.id  # member branch of accessible_predicate


async def test_detail_taken_down_fails_closed_even_for_member(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Gone", taken_down=True)
    await _community_member(session, c, viewer)  # even a member loses access
    with pytest.raises(svc.CommunityNotFound):
        await svc.community_detail(session, viewer_id=viewer.id, community_id=c.id)


async def test_detail_nonexistent_fails_closed(session):
    viewer = await _user(session, "viewer")
    with pytest.raises(svc.CommunityNotFound):
        await svc.community_detail(
            session, viewer_id=viewer.id, community_id=_ulid(999))


# =========================================================================
# Service layer — join (the authoritative gate)
# =========================================================================

async def test_join_public_creates_membership_and_bumps_count(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub", member_count=0)
    community, _, joined = await svc.join(
        session, viewer_id=viewer.id, community_id=c.id)
    assert joined is True
    assert community.member_count == 1
    m = (await session.execute(select(CommunityMembership).where(
        CommunityMembership.community_id == c.id,
        CommunityMembership.user_id == viewer.id))).scalar_one()
    assert m.role == "member"


async def test_join_is_idempotent_and_does_not_double_count(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub", member_count=0)
    await svc.join(session, viewer_id=viewer.id, community_id=c.id)
    community, _, joined = await svc.join(
        session, viewer_id=viewer.id, community_id=c.id)
    assert joined is False  # already in
    assert community.member_count == 1  # NOT 2
    count = len((await session.execute(select(CommunityMembership).where(
        CommunityMembership.community_id == c.id))).scalars().all())
    assert count == 1


async def test_join_returns_public_channels_to_subscribe(session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub")
    await _channel(session, cid=10, name="general", community_id=c.id)
    await _channel(session, cid=11, name="secret", community_id=c.id,
                   is_private=True)
    _, channels, _ = await svc.join(
        session, viewer_id=viewer.id, community_id=c.id)
    # public channels need no per-channel membership; private is hidden.
    assert {ch.name for ch in channels} == {"general"}
    # join records ONLY the community-grain membership, no per-channel rows.
    chan_members = (await session.execute(select(Membership))).scalars().all()
    assert chan_members == []


async def test_join_taken_down_fails_closed(session):
    """b2d9: the community was taken down between discover and join. join is the
    authoritative gate — it must reject, not honour the stale snapshot."""
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Gone", taken_down=True)
    with pytest.raises(svc.CommunityNotFound):
        await svc.join(session, viewer_id=viewer.id, community_id=c.id)
    # and no membership/no count leaked through the closed gate
    assert (await session.execute(select(CommunityMembership))).scalars().all() == []


async def test_join_private_non_member_fails_closed(session):
    """A private community is not self-joinable (B3 invites). Fail closed, and
    existence-hidden behind the same CommunityNotFound as nonexistent."""
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Cabal", visibility="private")
    with pytest.raises(svc.CommunityNotFound):
        await svc.join(session, viewer_id=viewer.id, community_id=c.id)


async def test_join_nonexistent_fails_closed(session):
    viewer = await _user(session, "viewer")
    with pytest.raises(svc.CommunityNotFound):
        await svc.join(session, viewer_id=viewer.id, community_id=_ulid(999))


# =========================================================================
# Service layer — my communities
# =========================================================================

async def test_list_mine_returns_only_my_communities(session):
    viewer = await _user(session, "viewer")
    a = await _community(session, cid=1, name="A")
    await _community(session, cid=2, name="B")  # not joined
    c = await _community(session, cid=3, name="C")
    await _community_member(session, a, viewer)
    await _community_member(session, c, viewer)
    rows = await svc.list_mine(session, viewer_id=viewer.id)
    assert [x.name for x in rows] == ["A", "C"]


async def test_list_mine_excludes_taken_down(session):
    viewer = await _user(session, "viewer")
    a = await _community(session, cid=1, name="A")
    gone = await _community(session, cid=2, name="Gone", taken_down=True)
    await _community_member(session, a, viewer)
    await _community_member(session, gone, viewer)
    rows = await svc.list_mine(session, viewer_id=viewer.id)
    assert [x.name for x in rows] == ["A"]


# =========================================================================
# REST seam — the wire contract + existence-hiding status mapping
# =========================================================================

def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(community_routes.router)
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


async def test_discover_endpoint_lists_public(client, session):
    viewer = await _user(session, "viewer")
    await _community(session, cid=1, name="Public")
    await _community(session, cid=2, name="Private", visibility="private")
    resp = await client.get("/v1/communities/discover",
                            headers=await _headers(viewer))
    assert resp.status_code == 200
    body = resp.json()
    assert [c["name"] for c in body["communities"]] == ["Public"]
    assert body["next_cursor"] is None


async def test_discover_route_not_shadowed_by_detail(client, session):
    """GET /v1/communities/discover must hit the directory, NOT be captured as a
    community with id='discover' (route-ordering regression guard)."""
    viewer = await _user(session, "viewer")
    resp = await client.get("/v1/communities/discover",
                            headers=await _headers(viewer))
    assert resp.status_code == 200
    assert "communities" in resp.json()  # directory shape, not a 404 detail


async def test_detail_endpoint_visible_200(client, session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub")
    await _channel(session, cid=10, name="general", community_id=c.id)
    resp = await client.get(f"/v1/communities/{c.id}",
                            headers=await _headers(viewer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == c.id
    assert [ch["name"] for ch in body["channels"]] == ["general"]


async def test_detail_endpoint_hidden_404(client, session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Cabal", visibility="private")
    resp = await client.get(f"/v1/communities/{c.id}",
                            headers=await _headers(viewer))
    assert resp.status_code == 404


async def test_join_endpoint_201_then_idempotent(client, session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Hub")
    first = await client.post(f"/v1/communities/{c.id}/join",
                              headers=await _headers(viewer))
    assert first.status_code == 201
    assert first.json()["joined"] is True
    second = await client.post(f"/v1/communities/{c.id}/join",
                               headers=await _headers(viewer))
    assert second.status_code == 201
    assert second.json()["joined"] is False  # idempotent re-join


async def test_join_endpoint_taken_down_404(client, session):
    viewer = await _user(session, "viewer")
    c = await _community(session, cid=1, name="Gone", taken_down=True)
    resp = await client.post(f"/v1/communities/{c.id}/join",
                             headers=await _headers(viewer))
    assert resp.status_code == 404


async def test_list_mine_endpoint(client, session):
    viewer = await _user(session, "viewer")
    a = await _community(session, cid=1, name="A")
    await _community_member(session, a, viewer)
    resp = await client.get("/v1/communities", headers=await _headers(viewer))
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["communities"]] == ["A"]


async def test_unauthenticated_rejected(client, session):
    await _community(session, cid=1, name="Public")
    resp = await client.get("/v1/communities/discover")  # no auth header
    assert resp.status_code in (401, 403)  # HTTPBearer auto-error
