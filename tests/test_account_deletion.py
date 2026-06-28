"""Account deletion (Apple 5.1.1(v)) — DELETE /v1/account.

Two layers, mirroring the rest of the suite:
  * service tests call `accounts_service.delete_user_account` directly and assert
    the cascade — user + federated identities + memberships gone, messages
    ANONYMIZED (unlinked, body preserved), and the sole-admin guard.
  * route tests drive `DELETE /v1/account` over HTTP and assert the observable
    contract: 204 on success, 401 unauthenticated, 409 when sole admin.

The app under test is built from JUST the auth routers (never `main`), keeping
the suite's "never import aiko_services" isolation invariant.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import accounts_service, security, users_service
from aiko_gateway.domain.models import Channel, Membership, Message, SocialIdentity
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session
from sqlalchemy import func, select


def _ulid(n: int) -> str:
    return f"{n:026d}"


async def _social_user(session, *, handle: str, sub: str):
    """A social-only user (password_hash=None) with one federated identity."""
    return await users_service.create_social_user(
        session, provider="google", provider_sub=sub,
        handle=handle, display_name=handle.title(), email=f"{handle}@example.com")


async def _channel(session, *, cid: int, name: str) -> Channel:
    ch = Channel(id=_ulid(cid), name=name, kind="standard", aiko_channel=name)
    session.add(ch)
    await session.commit()
    return ch


async def _member(session, *, channel: Channel, user, role: str) -> None:
    session.add(Membership(channel_id=channel.id, user_id=user.id, role=role))
    await session.commit()


async def _message(session, *, mid: int, channel: Channel, user) -> Message:
    msg = Message(
        id=_ulid(mid), channel_id=channel.id, sender_user_id=user.id,
        sender_kind="human", sender_label=user.display_name, body="hello world",
        created_at=dt.datetime(2026, 6, 28, tzinfo=dt.timezone.utc))
    session.add(msg)
    await session.commit()
    return msg


# --- service layer: the cascade ---------------------------------------------

async def test_delete_removes_user_identities_and_memberships(session):
    user = await _social_user(session, handle="alice", sub="g-alice")
    ch = await _channel(session, cid=1, name="general")
    await _member(session, channel=ch, user=user, role="member")

    await accounts_service.delete_user_account(session, user.id)

    assert await users_service.get_by_id(session, user.id) is None
    assert (await session.execute(
        select(func.count()).select_from(SocialIdentity)
        .where(SocialIdentity.user_id == user.id))).scalar_one() == 0
    assert (await session.execute(
        select(func.count()).select_from(Membership)
        .where(Membership.user_id == user.id))).scalar_one() == 0


async def test_delete_anonymizes_messages_preserving_body(session):
    user = await _social_user(session, handle="bob", sub="g-bob")
    ch = await _channel(session, cid=1, name="general")
    msg = await _message(session, mid=10, channel=ch, user=user)

    await accounts_service.delete_user_account(session, user.id)

    refreshed = await session.get(Message, msg.id)
    # The conversation survives; the account link and the human's name do not.
    assert refreshed is not None
    assert refreshed.body == "hello world"
    assert refreshed.sender_user_id is None
    assert refreshed.sender_label == accounts_service.DELETED_USER_LABEL


async def test_sole_admin_blocks_deletion_with_no_writes(session):
    user = await _social_user(session, handle="carol", sub="g-carol")
    ch = await _channel(session, cid=1, name="club")
    await _member(session, channel=ch, user=user, role="admin")  # the ONLY admin

    with pytest.raises(accounts_service.CannotDeleteSoleAdmin) as exc:
        await accounts_service.delete_user_account(session, user.id)
    assert ch.id in exc.value.channel_ids
    # The guard runs before any write — the account is untouched.
    assert await users_service.get_by_id(session, user.id) is not None


async def test_non_sole_admin_can_delete(session):
    user = await _social_user(session, handle="dave", sub="g-dave")
    other = await _social_user(session, handle="erin", sub="g-erin")
    ch = await _channel(session, cid=1, name="club")
    await _member(session, channel=ch, user=user, role="admin")
    await _member(session, channel=ch, user=other, role="admin")  # second admin

    await accounts_service.delete_user_account(session, user.id)

    assert await users_service.get_by_id(session, user.id) is None
    # The co-admin's membership is untouched.
    assert (await session.execute(
        select(func.count()).select_from(Membership)
        .where(Membership.user_id == other.id))).scalar_one() == 1


# --- route layer: the HTTP contract -----------------------------------------

@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.me_router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _auth(user) -> dict:
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


async def test_delete_account_unauthenticated_is_401(client, session):
    resp = await client.delete("/v1/account")
    assert resp.status_code == 401


async def test_delete_account_success_is_204(client, session):
    user = await _social_user(session, handle="frank", sub="g-frank")
    resp = await client.delete("/v1/account", headers=_auth(user))
    assert resp.status_code == 204
    assert await users_service.get_by_id(session, user.id) is None


async def test_delete_account_sole_admin_is_409(client, session):
    user = await _social_user(session, handle="grace", sub="g-grace")
    ch = await _channel(session, cid=1, name="club")
    await _member(session, channel=ch, user=user, role="admin")

    resp = await client.delete("/v1/account", headers=_auth(user))
    assert resp.status_code == 409
    # Rejected → the account still exists.
    assert await users_service.get_by_id(session, user.id) is not None
