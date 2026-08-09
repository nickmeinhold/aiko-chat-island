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
        identity="user-123", display_name="Robin", room="chan-abc")
    claims = _decode(tok)
    assert claims["iss"] == _LK_KEY          # SFU keys the secret off iss
    assert claims["sub"] == "user-123"       # participant identity == the id we passed
    assert claims["name"] == "Robin"
    grant = claims["video"]
    assert grant["room"] == "chan-abc"       # scoped to exactly one room
    assert grant["roomJoin"] is True
    assert grant["canPublish"] is True
    assert grant["canSubscribe"] is True
    # NO admin powers ever leak into a participant token.
    for admin in ("roomCreate", "roomAdmin", "roomList", "canUpdateOwnMetadata"):
        assert admin not in grant


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


async def _join(session, channel: Channel, user) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, can_post=True))
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
    body = resp.json()
    assert body["room"] == ch.id
    assert body["url"] == settings.livekit_url
    claims = _decode(body["token"])
    assert claims["sub"] == alice.id           # server-derived identity (I5)
    assert claims["video"]["room"] == ch.id


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
