"""LiveKit video-token minting + the room-access trust boundary.

Two layers, mirroring the codebase's split:
  * DOMAIN (livekit_tokens.mint_room_token): the token is a correctly-scoped HS256
    LiveKit grant — identity is the passed id, room is the passed room, powers are
    join+publish+subscribe and NOTHING admin, and an unconfigured island refuses
    to mint (raises, never signs with an empty secret).
  * ROUTE (POST /v1/channels/{id}/video-token): the ACL gate — a member gets a
    token scoped to the channel-as-room under their OWN identity; a non-member of a
    private channel and a missing channel are the SAME existence-hiding 404; an
    unauthenticated caller is rejected; an unconfigured island returns 503 not 500.

App-under-test is built from JUST the needed routers (never `main`) to keep the
suite's "never import aiko_services" isolation invariant — same pattern as
test_membership_acl.
"""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.domain import livekit_tokens, security, users_service
from aiko_gateway.domain.models import Channel, Membership
from aiko_gateway.rest import channels as channel_routes
from aiko_gateway.rest import livekit as livekit_routes
from aiko_gateway.rest.deps import get_session

# A >= 32-byte secret so PyJWT's HS256 machinery is happy (same posture as the
# harness JWT_SECRET). LiveKit itself accepts any secret; this is just the encoder.
_LK_KEY = "APItestkey000000"
_LK_SECRET = "livekit-test-secret-at-least-32-bytes!!"


@pytest.fixture
def livekit_configured(monkeypatch):
    """Configure the island with LiveKit creds for the duration of a test. Patches
    the settings SINGLETON that livekit_tokens reads at call time, so no re-import
    is needed; monkeypatch restores the originals afterward."""
    monkeypatch.setattr(settings, "livekit_api_key", _LK_KEY)
    monkeypatch.setattr(settings, "livekit_api_secret", _LK_SECRET)
    return settings


def _ulid(n: int) -> str:
    return f"{n:026d}"


def _decode(token: str) -> dict:
    """Decode a minted LiveKit token AS LIVEKIT WOULD: verify the signature with the
    shared secret and require HS256. A token that doesn't verify here would be
    rejected by the SFU — so this doubles as a 'the SFU will accept it' check."""
    return jwt.decode(token, _LK_SECRET, algorithms=["HS256"])


# =========================================================================
# DOMAIN — mint_room_token scoping + fail-closed-when-unconfigured
# =========================================================================

def test_mint_binds_identity_room_and_scopes_grant(livekit_configured):
    tok = livekit_tokens.mint_room_token(
        identity="user-123", display_name="Robin", room="chan-abc", can_publish=True)
    claims = _decode(tok)
    assert claims["iss"] == _LK_KEY          # SFU keys the secret off iss
    assert claims["sub"] == "user-123"       # participant identity == the id we passed
    assert claims["name"] == "Robin"
    grant = claims["video"]
    assert grant["room"] == "chan-abc"       # scoped to exactly one room
    assert grant["roomJoin"] is True
    assert grant["canPublish"] is True       # explicitly opted up
    assert grant["canSubscribe"] is True
    assert grant["canPublishSources"] == ["camera", "microphone"]  # A/V only, no screen-share
    # NO admin powers ever leak into a participant token.
    for admin in ("roomCreate", "roomAdmin", "roomList", "canUpdateOwnMetadata"):
        assert admin not in grant


def test_mint_subscribe_only_omits_publish_sources(livekit_configured):
    # Not publishing → canPublishSources omitted (canPublish=False already denies all).
    grant = _decode(livekit_tokens.mint_room_token(
        identity="u", display_name="U", room="r"))["video"]
    assert grant["canPublish"] is False
    assert "canPublishSources" not in grant


def test_mint_rejects_empty_identity_or_room(livekit_configured):
    with pytest.raises(ValueError, match="identity"):
        livekit_tokens.mint_room_token(identity="  ", display_name="U", room="r")
    with pytest.raises(ValueError, match="room"):
        livekit_tokens.mint_room_token(identity="u", display_name="U", room="")


def test_mint_defaults_are_least_privilege(livekit_configured):
    # THE cage-match #122 rd2 finding (Tesla+Wu F1): the DEFAULT grant — a bare
    # three-kwarg mint by any future caller — must be subscribe-only, never full A/V
    # + data. Safe by default in the door, opt UP explicitly.
    claims = _decode(livekit_tokens.mint_room_token(
        identity="u", display_name="U", room="r"))
    grant = claims["video"]
    assert grant["canSubscribe"] is True
    assert grant["canPublish"] is False       # NOT broadcasting by default
    assert grant["canPublishData"] is False   # data side-channel closed by default


def test_mint_ttl_is_bounded_and_future(livekit_configured):
    before = dt.datetime.now(dt.timezone.utc)
    claims = _decode(livekit_tokens.mint_room_token(
        identity="u", display_name="U", room="r"))
    exp = dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc)
    delta = (exp - before).total_seconds()
    assert 0 < delta <= settings.livekit_token_ttl_seconds + 5


def test_mint_can_narrow_powers():
    # A subscribe-only grant (e.g. a viewer that must not publish) drops canPublish.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "livekit_api_key", _LK_KEY)
        mp.setattr(settings, "livekit_api_secret", _LK_SECRET)
        claims = _decode(livekit_tokens.mint_room_token(
            identity="u", display_name="U", room="r",
            can_publish=False, can_publish_data=False))
    assert claims["video"]["canPublish"] is False
    assert claims["video"]["canSubscribe"] is True
    assert claims["video"]["canPublishData"] is False


def test_mint_unconfigured_raises_not_signs(monkeypatch):
    # An island with no creds must REFUSE to mint, never sign with an empty secret.
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    assert livekit_tokens.is_configured() is False
    with pytest.raises(livekit_tokens.LiveKitNotConfigured):
        livekit_tokens.mint_room_token(identity="u", display_name="U", room="r")


# =========================================================================
# ROUTE — ACL gate + existence-hiding + server-derived identity
# =========================================================================

@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(channel_routes.router)
    app.include_router(livekit_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _user(session, username: str):
    return await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")


async def _private_channel(session, *, cid: int = 10, name: str = "dm") -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="dm", aiko_channel=name, is_private=True)
    session.add(ch)
    await session.commit()
    return ch


async def _public_channel(session, *, cid: int = 20, name: str = "general") -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name, is_private=False)
    session.add(ch)
    await session.commit()
    return ch


async def _join(session, channel: Channel, user, *, can_post: bool = True) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, can_post=can_post))
    await session.commit()


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_member_gets_token_scoped_to_channel_under_own_identity(
    client, session, livekit_configured
):
    alice = await _user(session, "alice")
    ch = await _private_channel(session)
    await _join(session, ch, alice)

    resp = await client.post(
        f"/v1/channels/{ch.id}/video-token", headers=_headers(alice))
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"   # bearer credential, never cached
    body = resp.json()
    assert body["room"] == ch.id
    assert body["url"] == settings.livekit_url
    assert body["can_publish"] is True                        # a posting member may publish
    claims = _decode(body["token"])
    assert claims["sub"] == alice.id           # server-derived identity (I5)
    assert claims["video"]["room"] == ch.id
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canPublishData"] is False         # data side-channel off by default


async def test_read_only_member_gets_subscribe_only_token(
    client, session, livekit_configured
):
    # THE cage-match #122 finding (Carnot/Tesla/Wu/Maxwell): read access must NOT mint
    # a publish capability. A member with can_post=False can read but not post text —
    # so the video token must be subscribe-only, never canPublish.
    mute = await _user(session, "mute")
    ch = await _private_channel(session)
    await _join(session, ch, mute, can_post=False)

    resp = await client.post(
        f"/v1/channels/{ch.id}/video-token", headers=_headers(mute))
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_publish"] is False
    claims = _decode(body["token"])
    assert claims["video"]["canPublish"] is False             # cannot broadcast
    assert claims["video"]["canSubscribe"] is True            # can still watch/listen
    assert claims["video"]["canPublishData"] is False


async def test_gateway_id_namespaces_room_and_identity(
    client, session, livekit_configured, monkeypatch
):
    # cage-match #122 rd2 (Tesla+Wu F2): the prefixed branch was never exercised. With
    # gateway_id set, BOTH room and participant identity carry the island prefix so
    # they can't collide across islands sharing one SFU/API key.
    monkeypatch.setattr(settings, "gateway_id", "island-a")
    alice = await _user(session, "alice")
    ch = await _private_channel(session)
    await _join(session, ch, alice)

    resp = await client.post(
        f"/v1/channels/{ch.id}/video-token", headers=_headers(alice))
    assert resp.status_code == 200
    body = resp.json()
    assert body["room"] == f"island-a:{ch.id}"
    claims = _decode(body["token"])
    assert claims["sub"] == f"island-a:{alice.id}"       # identity namespaced too
    assert claims["video"]["room"] == f"island-a:{ch.id}"


async def test_public_channel_non_member_is_subscribe_only(client, session, livekit_configured):
    # cage-match #122 rd3 (Wu #3): a public channel is READABLE by any signed-in user,
    # but live broadcast must NOT be open to every non-member — publish requires an
    # explicit posting membership. So a non-member reader gets a subscribe-only token
    # (200, can_publish False), NOT a 404 and NOT canPublish.
    randomer = await _user(session, "randomer")
    pub = await _public_channel(session)  # no membership row for randomer

    resp = await client.post(
        f"/v1/channels/{pub.id}/video-token", headers=_headers(randomer))
    assert resp.status_code == 200                 # public → readable → can join
    body = resp.json()
    assert body["can_publish"] is False            # ...but subscribe-only
    claims = _decode(body["token"])
    assert claims["video"]["canPublish"] is False
    assert claims["video"]["canSubscribe"] is True


async def test_public_channel_posting_member_can_publish(client, session, livekit_configured):
    # cage-match #122 rd4 (Wu #3): the positive public path — a public-channel member
    # WITH a can_post=True row DOES get canPublish. Proves the feature works on a
    # public channel once memberships are provisioned (not only on private DMs).
    speaker = await _user(session, "speaker")
    pub = await _public_channel(session)
    await _join(session, pub, speaker, can_post=True)

    resp = await client.post(
        f"/v1/channels/{pub.id}/video-token", headers=_headers(speaker))
    assert resp.status_code == 200
    assert resp.json()["can_publish"] is True
    assert _decode(resp.json()["token"])["video"]["canPublish"] is True


async def test_non_member_of_private_channel_is_404(client, session, livekit_configured):
    alice = await _user(session, "alice")
    bob = await _user(session, "bob")
    ch = await _private_channel(session)
    await _join(session, ch, alice)            # bob is NOT a member

    resp = await client.post(
        f"/v1/channels/{ch.id}/video-token", headers=_headers(bob))
    assert resp.status_code == 404


async def test_missing_channel_is_same_404(client, session, livekit_configured):
    alice = await _user(session, "alice")
    resp = await client.post(
        f"/v1/channels/{_ulid(999)}/video-token", headers=_headers(alice))
    # Identical to the non-member case — no existence leak.
    assert resp.status_code == 404


async def test_unauthenticated_is_rejected(client, session, livekit_configured):
    ch = await _private_channel(session)
    resp = await client.post(f"/v1/channels/{ch.id}/video-token")
    assert resp.status_code in (401, 403)


async def test_unconfigured_island_returns_503_not_500(client, session, monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "")
    monkeypatch.setattr(settings, "livekit_api_secret", "")
    alice = await _user(session, "alice")
    ch = await _private_channel(session)
    await _join(session, ch, alice)

    resp = await client.post(
        f"/v1/channels/{ch.id}/video-token", headers=_headers(alice))
    assert resp.status_code == 503
