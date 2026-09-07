"""Call occupancy — `GET /v1/channels/{id}/call` (#3159), the ring's liveness gate.

A signed call invitation is a permanent claim about the PAST; whether the call is still
happening is a fact about the PRESENT. This endpoint is the present-tense half, and the
tests below are organised around the three things that can go wrong with it:

  * **The admin door is wider than the join door**, so it is tested for exactly the
    powers it should have and the ones it must never acquire.
  * **Unknown must not read as empty.** The one behaviour whose inversion silently
    breaks the feature it exists to provide: an unreachable SFU reporting `live: false`
    would cancel a call that is genuinely happening. There is a dedicated must-fail arm
    for it — delete the `except` branch's 503 and that test goes red.
  * **The gate must not drift from video-token's.** Two endpoints about one object; if
    their gates diverge the weaker becomes an oracle for what the stronger hides. That
    is asserted by running both endpoints through the same scenarios and comparing.

App-under-test is built from JUST the needed routers (never `main`) to keep the suite's
"never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import null

from aiko_gateway.config import settings
from aiko_gateway.domain import (
    livekit_rooms, livekit_tokens, moderation_service, security, users_service,
)
from aiko_gateway.domain.models import Channel, Membership
from aiko_gateway.rest import livekit as livekit_routes
from aiko_gateway.rest.deps import get_session

_LK_KEY = "APItestkey000000"
_LK_SECRET = "livekit-test-secret-at-least-32-bytes!!"


@pytest.fixture
def livekit_configured(monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", _LK_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", _LK_SECRET)
    monkeypatch.setattr(settings, "island_id", "")
    monkeypatch.setattr(settings, "livekit_url", "wss://sfu.example.test")
    return settings


def _ulid(n: int) -> str:
    return f"{n:026d}"


def _decode(token: str) -> dict:
    return jwt.decode(token, _LK_SECRET, algorithms=["HS256"])


def _fake_sfu(monkeypatch, handler):
    """Point livekit_rooms at a MockTransport instead of the network.

    Patches ``httpx.AsyncClient`` in the MODULE's namespace rather than adding a
    production injection seam — the code under test stays exactly what ships.

    The module pools ONE client (see its ``_client``/``aclose``, mirroring
    ``domain/apns.py``), so the singleton is also reset here: without it the first
    test to run would build the pooled client and every later test would silently
    reuse THAT one, making its own handler dead code and its assertions vacuous —
    a whole file of tests grading one stale mock. ``monkeypatch.setattr`` restores
    the previous value afterwards, so the reset is symmetric.
    """
    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(livekit_rooms, "_client_singleton", None)
    monkeypatch.setattr(livekit_rooms.httpx, "AsyncClient", _factory)


def _responds(payload, status_code=200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.last_request = request
        if isinstance(payload, str):
            return httpx.Response(status_code, text=payload)
        return httpx.Response(status_code, json=payload)
    handler.last_request = None
    return handler


# =========================================================================
# DOMAIN — the admin door
# =========================================================================

def test_admin_token_is_scoped_to_one_room_and_cannot_join(livekit_configured):
    grant = _decode(livekit_tokens.mint_room_admin_token(room="chan-abc"))["video"]
    assert grant["roomAdmin"] is True
    assert grant["room"] == "chan-abc"    # SFU-enforced scope: this room, no other
    assert grant["roomJoin"] is False     # an admin token can never enter a room
    # The powers a JOIN token carries must never appear on this one, and vice versa.
    for participant_power in ("canPublish", "canSubscribe", "canPublishData",
                              "canPublishSources"):
        assert participant_power not in grant
    # Nor the broad ones. roomList in particular would let one token enumerate every
    # room on a SHARED SFU, including other islands'.
    for broad in ("roomList", "roomCreate", "roomRecord"):
        assert broad not in grant


def test_admin_token_is_namespaced_like_a_join_token(monkeypatch, livekit_configured):
    monkeypatch.setattr(settings, "island_id", "island-a")
    grant = _decode(livekit_tokens.mint_room_admin_token(room="chan-abc"))["video"]
    # An admin token can no more escape the island's namespace than a join token can.
    assert grant["room"] == "island-a:chan-abc"


def test_admin_token_ttl_is_much_shorter_than_a_join_token(livekit_configured):
    before = dt.datetime.now(dt.timezone.utc)
    claims = _decode(livekit_tokens.mint_room_admin_token(room="r"))
    delta = (dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc) - before).total_seconds()
    assert 0 < delta <= 70
    # The point of the short TTL: this grant permits mutations we never make, so a
    # leaked admin token must expire far sooner than a leaked join token.
    assert delta < settings.livekit_token_ttl_seconds


def test_admin_token_refuses_when_unconfigured_or_roomless(monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    with pytest.raises(livekit_tokens.LiveKitNotConfigured):
        livekit_tokens.mint_room_admin_token(room="r")
    monkeypatch.setattr(settings, "livekit_api_key", _LK_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", _LK_SECRET)
    with pytest.raises(ValueError, match="room"):
        livekit_tokens.mint_room_admin_token(room="   ")


# =========================================================================
# DOMAIN — reading occupancy
# =========================================================================

async def test_empty_room_is_not_live_and_has_no_since(monkeypatch, livekit_configured):
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.live is False
    assert occ.participants == 0
    assert occ.since is None


async def test_occupied_room_reports_count_and_earliest_join(monkeypatch, livekit_configured):
    _fake_sfu(monkeypatch, _responds({"participants": [
        {"identity": "a", "joinedAt": "1788700000"},
        {"identity": "b", "joinedAt": "1788700042"},
    ]}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.live is True
    assert occ.participants == 2
    # EARLIEST current join, not the later one: "since" answers when this occupancy
    # began, so a callee can tell "still the call I was rung for" from a newer one.
    assert occ.since == "2026-09-06T13:06:40Z"   # rendered INSIDE the 503 boundary


async def test_absent_or_zero_joined_at_never_backdates_since(monkeypatch, livekit_configured):
    # An unstamped participant must not make the call look like it started in 1970 —
    # which is what a naive min() over int(p.get("joinedAt", 0)) would produce, and it
    # would defeat the app's "is this the same call" comparison rather than just being
    # untidy.
    _fake_sfu(monkeypatch, _responds({"participants": [
        {"identity": "a"},                       # absent  -> protobuf "no value"
        {"identity": "b", "joinedAt": 0},        # zero    -> protobuf "no value"
        {"identity": "c", "joinedAt": "1788700500"},
    ]}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.participants == 3          # all three ARE present
    assert occ.since == "2026-09-06T13:15:00Z"


async def test_every_participant_unstamped_is_live_with_no_since(
    monkeypatch, livekit_configured
):
    """Unstamped is ORDINARY, not broken: the room is occupied and we simply cannot say
    since when. This is the arm that keeps the r3 strictness from over-reaching — if
    absent/zero were treated as malformed, a legitimately unstamped room would 503."""
    _fake_sfu(monkeypatch, _responds({"participants": [{"identity": "a"}, {"identity": "b"}]}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.live is True and occ.participants == 2 and occ.since is None


async def test_request_carries_the_namespaced_room_and_a_bearer(
    monkeypatch, livekit_configured
):
    monkeypatch.setattr(settings, "island_id", "island-a")
    handler = _responds({"participants": []})
    _fake_sfu(monkeypatch, handler)
    await livekit_rooms.occupancy(room="chan-abc")
    req = handler.last_request
    # wss:// client URL -> https:// API origin, same host, no second setting to drift.
    assert str(req.url) == "https://sfu.example.test/twirp/livekit.RoomService/ListParticipants"
    assert b'"island-a:chan-abc"' in req.content
    # The body's room and the TOKEN's room must name the same thing, or the SFU rejects
    # the pair — assert they agree rather than trusting they do.
    token = req.headers["Authorization"].removeprefix("Bearer ")
    assert _decode(token)["video"]["room"] == "island-a:chan-abc"


@pytest.mark.parametrize("arm,handler", [
    ("http 500", _responds({"error": "boom"}, status_code=500)),
    ("http 401", _responds({"msg": "permissions denied"}, status_code=401)),
    ("non-JSON body", _responds("<html>gateway</html>")),
])
async def test_sfu_failures_raise_rather_than_reporting_empty(
    monkeypatch, livekit_configured, arm, handler
):
    _fake_sfu(monkeypatch, handler)
    with pytest.raises(livekit_rooms.LiveKitUnreachable):
        await livekit_rooms.occupancy(room="chan-abc")


async def test_network_error_raises_rather_than_reporting_empty(
    monkeypatch, livekit_configured
):
    def boom(request):
        raise httpx.ConnectTimeout("no route", request=request)
    _fake_sfu(monkeypatch, boom)
    with pytest.raises(livekit_rooms.LiveKitUnreachable):
        await livekit_rooms.occupancy(room="chan-abc")


# =========================================================================
# ROUTE — gates, and the one inversion that breaks the feature
# =========================================================================

@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(livekit_routes.router)
    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _dm(session, *, cid: int = 10) -> Channel:
    ch = Channel(id=_ulid(cid), name="dm", kind="dm", aiko_channel=f"dm:{cid}",
                 is_private=True, community_id=null())
    session.add(ch)
    await session.commit()
    return ch


async def _public(session, *, cid: int = 20) -> Channel:
    ch = Channel(id=_ulid(cid), name="general", kind="standard",
                 aiko_channel="general", is_private=False)
    session.add(ch)
    await session.commit()
    return ch


async def _join(session, channel: Channel, user, *, can_post: bool = True) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, can_post=can_post))
    await session.commit()


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_member_sees_live_call_with_count_only(
    client, session, monkeypatch, livekit_configured
):
    alice, peer = await _user(session, "alice"), await _user(session, "peer")
    ch = await _dm(session)
    await _join(session, ch, alice)
    await _join(session, ch, peer)
    _fake_sfu(monkeypatch, _responds({"participants": [
        {"identity": peer.id, "joinedAt": "1788700000"},
    ]}))

    resp = await client.get(f"/v1/channels/{ch.id}/call", headers=_headers(alice))
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True
    assert body["participants"] == 1
    assert body["since"] == "2026-09-06T13:06:40Z"   # 1788700000 epoch seconds, computed not guessed
    # COUNT ONLY: no identity may reach the wire, or this becomes a presence probe.
    assert peer.id not in resp.text
    # A ring polls this; a cached answer is a stale answer, which is the bug itself.
    assert resp.headers.get("cache-control") == "no-store"


async def test_unreachable_sfu_is_503_NEVER_live_false(
    client, session, monkeypatch, livekit_configured
):
    """THE must-fail arm. Collapsing "cannot tell" into "nobody is here" would tell a
    ringing handset the call had ended and cancel a call that is genuinely happening —
    the exact failure this endpoint exists to prevent, inverted. Delete the 503 in the
    route's except branch and this goes red."""
    alice, peer = await _user(session, "alice"), await _user(session, "peer")
    ch = await _dm(session)
    await _join(session, ch, alice)
    await _join(session, ch, peer)

    def boom(request):
        raise httpx.ConnectTimeout("no route", request=request)
    _fake_sfu(monkeypatch, boom)

    resp = await client.get(f"/v1/channels/{ch.id}/call", headers=_headers(alice))
    assert resp.status_code == 503
    assert "live" not in resp.json()      # no fabricated verdict rides along


async def test_unconfigured_island_is_503(client, session, monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    alice = await _user(session, "alice")
    ch = await _dm(session)
    await _join(session, ch, alice)
    resp = await client.get(f"/v1/channels/{ch.id}/call", headers=_headers(alice))
    assert resp.status_code == 503


async def test_unauthenticated_is_rejected(client, session, livekit_configured):
    ch = await _dm(session)
    assert (await client.get(f"/v1/channels/{ch.id}/call")).status_code in (401, 403)


# ---- gate parity with video-token: the property, not a list of cases ----

async def _both(client, user, channel_id):
    """Hit both endpoints as the same caller and return their status codes."""
    occ = await client.get(f"/v1/channels/{channel_id}/call", headers=_headers(user))
    tok = await client.post(f"/v1/channels/{channel_id}/video-token",
                            headers=_headers(user))
    return occ.status_code, tok.status_code


async def test_gates_agree_missing_channel(client, session, monkeypatch, livekit_configured):
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    alice = await _user(session, "alice")
    occ, tok = await _both(client, alice, _ulid(999))
    assert occ == tok == 404


async def test_gates_agree_non_member_of_private_channel(
    client, session, monkeypatch, livekit_configured
):
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    outsider = await _user(session, "outsider")
    a, b = await _user(session, "a"), await _user(session, "b")
    ch = await _dm(session)
    await _join(session, ch, a)
    await _join(session, ch, b)
    occ, tok = await _both(client, outsider, ch.id)
    # Existence-hiding: identical to "no such channel" above, on BOTH endpoints.
    assert occ == tok == 404


async def test_gates_agree_public_channel_is_dm_only(
    client, session, monkeypatch, livekit_configured
):
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    alice = await _user(session, "alice")
    ch = await _public(session)
    await _join(session, ch, alice)
    occ, tok = await _both(client, alice, ch.id)
    assert occ == tok == 403


async def test_gates_agree_blocked_peer_cannot_observe_the_call(
    client, session, monkeypatch, livekit_configured
):
    """A block must traverse the OCCUPANCY path exactly as it traverses the token path.
    If it did not, a blocked user could poll this endpoint and learn precisely when the
    person who blocked them is on a call — a live presence feed built out of the safety
    boundary that was supposed to close."""
    alice, blocked = await _user(session, "alice"), await _user(session, "blocked")
    ch = await _dm(session)
    await _join(session, ch, alice)
    await _join(session, ch, blocked)
    await moderation_service.block_user(session, blocker_id=alice.id,
                                        blocked_id=blocked.id)
    # The SFU says a call IS happening — so a leak here would be a real disclosure,
    # not a vacuous pass against an empty room.
    _fake_sfu(monkeypatch, _responds({"participants": [
        {"identity": alice.id, "joinedAt": "1788700000"}]}))
    occ, tok = await _both(client, blocked, ch.id)
    assert occ == tok == 404


async def test_gates_agree_three_party_dm_fails_closed(
    client, session, monkeypatch, livekit_configured
):
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    a, b, c = (await _user(session, "a"), await _user(session, "b"),
               await _user(session, "c"))
    ch = await _dm(session)
    for u in (a, b, c):
        await _join(session, ch, u)
    occ, tok = await _both(client, a, ch.id)
    assert occ == tok == 403


# ---- the vendor boundary: shapes we do not understand are UNKNOWN, not EMPTY ----
# Found by Carnot in the cage-match on PR #167, confirmed executably before the fix.
# The first parser read `resp.json().get("participants") or []`, which turned a
# missing/null field into an empty room and therefore into `live: false` on a
# ringing handset — the module's own stated principle, violated by its own parser.

@pytest.mark.parametrize("arm,body", [
    ("participants key MISSING",   {}),
    ("participants is null",       {"participants": None}),
    ("participants is an object",  {"participants": {"a": {"joinedAt": "1"}}}),
    ("participants is a string",   {"participants": "alice"}),
    ("participants holds strings", {"participants": ["alice"]}),
    ("participants holds ints",    {"participants": [42]}),
    ("body is a list",             ["nope"]),
    ("body is a string",           "nope"),
])
async def test_unrecognised_payload_is_unknown_not_empty(
    monkeypatch, livekit_configured, arm, body
):
    """Every one of these previously returned live=False (or raised AttributeError
    and escaped as a 500). All must now be the same 'cannot determine' as a network
    failure: there is ONE meaning for 'the answer did not arrive', regardless of
    which way it failed to."""
    _fake_sfu(monkeypatch, _responds(body))
    with pytest.raises(livekit_rooms.LiveKitUnreachable):
        await livekit_rooms.occupancy(room="chan-abc")


async def test_the_empty_ARRAY_is_still_a_real_empty_room(
    monkeypatch, livekit_configured
):
    """THE NULL CONTROL for the test above, and it is load-bearing rather than
    decorative. Requiring the `participants` key is only safe because LiveKit emits
    it even when empty — protobuf-JSON often OMITS empty repeated fields, and if it
    did here, the strictness above would 503 every empty room and break the feature
    outright. Verified at the byte level against both live islands 2026-09-07:
    an empty room returns literally {"participants":[]}. This test pins the
    distinction the fix rests on — empty array is data, absent key is ignorance."""
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.live is False and occ.participants == 0 and occ.since is None


async def test_malformed_payload_reaches_the_route_as_503_not_500(
    client, session, monkeypatch, livekit_configured
):
    """The route promises 503 for 'cannot tell'. A malformed-but-valid-JSON body used
    to raise AttributeError past the route's `except` and surface as a 500 — a
    different code, a different client code path, and an alarm that reads as a
    gateway bug rather than an SFU one."""
    alice, peer = await _user(session, "alice"), await _user(session, "peer")
    ch = await _dm(session)
    await _join(session, ch, alice)
    await _join(session, ch, peer)
    _fake_sfu(monkeypatch, _responds({"participants": ["alice"]}))

    resp = await client.get(f"/v1/channels/{ch.id}/call", headers=_headers(alice))
    assert resp.status_code == 503
    assert "live" not in resp.json()


async def test_aclose_releases_the_pooled_client(monkeypatch, livekit_configured):
    """The pooled client is closed by the app lifespan. If aclose() failed to drop the
    reference, a second lifespan (a test harness, an embedded server) would hand out a
    CLOSED client forever."""
    _fake_sfu(monkeypatch, _responds({"participants": []}))
    await livekit_rooms.occupancy(room="chan-abc")
    assert livekit_rooms._client_singleton is not None      # pooled after first use
    await livekit_rooms.aclose()
    assert livekit_rooms._client_singleton is None          # and released on shutdown


# ---- the CLASS round 3 closed: no SFU-derived value is computed after the boundary ----
# Round 1's instance was SHAPE (a malformed body raised AttributeError -> 500). Round 3's
# was RANGE: the route called datetime.fromtimestamp on the SFU's joinedAt OUTSIDE the try
# that maps SFU failure to 503, so an absurd stamp raised OverflowError and escaped as a
# gateway 500. Same class both times -- SFU data being computed past the boundary meant to
# contain it. Rendering now happens in the domain, so there is nothing left outside to break.

@pytest.mark.parametrize("arm,stamp", [
    ("past platform time_t", "999999999999999999999"),
    ("negative",             "-1788700000"),
    ("unparseable",          "1e400"),
    ("not a number at all",  "yesterday"),
])
async def test_unrenderable_joined_at_is_503_not_a_gateway_500(
    monkeypatch, livekit_configured, arm, stamp
):
    _fake_sfu(monkeypatch, _responds({"participants": [{"joinedAt": stamp}]}))
    with pytest.raises(livekit_rooms.LiveKitUnreachable):
        await livekit_rooms.occupancy(room="chan-abc")


async def test_unrenderable_joined_at_reaches_the_route_as_503(
    client, session, monkeypatch, livekit_configured
):
    """The route-level proof. Before the fix this was an uncaught OverflowError -> 500,
    which is a different status, a different client path, and an alarm that reads as a
    gateway bug rather than an SFU one."""
    alice, peer = await _user(session, "alice"), await _user(session, "peer")
    ch = await _dm(session)
    await _join(session, ch, alice)
    await _join(session, ch, peer)
    _fake_sfu(monkeypatch, _responds({"participants": [
        {"identity": peer.id, "joinedAt": "999999999999999999999"}]}))

    resp = await client.get(f"/v1/channels/{ch.id}/call", headers=_headers(alice))
    assert resp.status_code == 503
    assert "live" not in resp.json()


async def test_a_sane_stamp_still_renders(monkeypatch, livekit_configured):
    """NULL CONTROL for the three arms above. A range check that rejects everything
    would pass them all while breaking the feature — this pins that ordinary
    timestamps are still accepted and rendered."""
    _fake_sfu(monkeypatch, _responds({"participants": [{"joinedAt": "1788700000"}]}))
    occ = await livekit_rooms.occupancy(room="chan-abc")
    assert occ.since == "2026-09-06T13:06:40Z"
