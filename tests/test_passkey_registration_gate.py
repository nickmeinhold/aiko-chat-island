"""`/v1/auth/passkey/register/*` is gated by `settings.open_registration`.

THE HOLE THIS CLOSES. `/v1/auth/register` has been gated since task #38, and
`open_registration` is force-closed in production precisely because I2 membership
(#36) is unenforced — a self-registered account can read everything. The PASSKEY
door was never given the same check, so the gate covered the door nobody uses and
left open the one the app actually ships. Measured against the live island on
2026-09-15: an unauthenticated `POST /v1/auth/passkey/register/start` returned 200
with an account handle allocated, while `/v1/auth/register` returned 403 in the
same breath.

BOTH HALVES ARE GATED, not just `finish`. `finish` is the chokepoint that creates
the account and is the one that must hold; `start` is gated too so a closed island
says so before a user completes a biometric ceremony that cannot succeed. A caller
that skips `start` still meets the gate at `finish`.

`add/finish` is deliberately NOT gated: it requires `CurrentUser` and attaches a
credential to an account that already exists. Closing registration must not stop
an existing user adding a device — that is the difference between "no new
accounts" and "no new keys", and only the first was decided.
"""
from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiko_gateway.config import settings
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_session


@pytest_asyncio.fixture
async def client(session):
    async def _override_session():
        yield session

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# A syntactically valid body that would fail verification LATER. The point is that
# a closed island must refuse BEFORE reaching verification, so this never needs to
# be a real attestation — and if the gate regressed, this body's own failure mode
# (400/401) is distinguishable from the 403 being asserted.
_FINISH_BODY = {"state": "not-a-real-challenge", "credential": {"id": "x"}}


async def test_passkey_register_start_is_refused_when_registration_is_closed(
        client, monkeypatch):
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/register/start")
    assert resp.status_code == 403, (
        "a closed island issued a registration challenge — this is the live hole: "
        "an anonymous caller could complete it and get an account")


async def test_passkey_register_finish_is_refused_when_registration_is_closed(
        client, monkeypatch):
    """The chokepoint. Must hold even for a caller that never called `start`."""
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/register/finish", json=_FINISH_BODY)
    assert resp.status_code == 403, (
        "a closed island accepted an account-creating attestation; a 400/401 here "
        "would mean the gate is absent and the request merely failed verification")


async def test_passkey_register_start_works_when_registration_is_open(
        client, monkeypatch):
    """THE NEGATIVE CONTROL. Without this, a gate that refused unconditionally
    would pass both tests above and silently break every island that WANTS open
    registration — including every dev environment, where the default is True."""
    monkeypatch.setattr(settings, "open_registration", True)
    resp = await client.post("/v1/auth/passkey/register/start")
    assert resp.status_code == 200
    body = resp.json()
    assert "state" in body and "options" in body


async def test_passkey_add_finish_is_NOT_gated_by_open_registration(
        client, monkeypatch):
    """Adding a key to an EXISTING account is not registration.

    Asserted by its failure MODE, not its success: unauthenticated it must answer
    401 (no session), never 403 (registration closed). A 403 here would mean the
    sweep over-reached and locked existing users out of adding a device.
    """
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/add/finish", json=_FINISH_BODY)
    assert resp.status_code != 403, (
        "add/finish was caught by the registration gate — closing signup must not "
        "stop an existing user adding a passkey")
    assert resp.status_code == 401, (
        f"expected 401 (no session), got {resp.status_code}")


async def test_passkey_authenticate_start_is_NOT_gated(client, monkeypatch):
    """Existing users must still be able to SIGN IN on a closed island."""
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/authenticate/start")
    assert resp.status_code != 403, (
        "closing registration locked out existing users — sign-in is not signup")


async def test_social_claim_is_exempt_from_open_registration(client, monkeypatch):
    """THE EXEMPTION, PINNED WITH A **VALID** PROVISIONING TOKEN — because with a
    bogus one this test cannot fail for the reason it names.

    `/social/claim` decodes the token FIRST and answers 401 on a bad one, before
    either gate. A version of this test using `"not-a-real-token"` passed while
    never reaching the provisioning door at all: a check whose success value is
    indistinguishable from its disabled value, which is the exact class this
    session has spent the day removing. Caught before merge; recorded so it is not
    reintroduced.

    WHY THE EXEMPTION EXISTS: `config.py` REJECTS `open_registration=True` in
    production, while social sign-in MAY be enabled there (Nick, 2026-06-27). A
    cage-match panel (Kelvin + Carnot, independently) prescribed routing social
    through `open_registration`; that would make social sign-in permanently unable
    to provision an account in production and silently retire that decision. The
    panel could not see it — `config.py` is not in this diff.

    So: registration CLOSED, social ENABLED, a REAL provisioning token. The claim
    must succeed. If it ever answers "registration is closed", the exemption has
    been lost.
    """
    from aiko_gateway.domain import security

    monkeypatch.setattr(settings, "open_registration", False)
    monkeypatch.setattr(settings, "social_signin_enabled", True)
    prov = security.issue_provisioning("google", "sub-exempt-check")
    resp = await client.post("/v1/auth/social/claim", json={
        "provisioning_token": prov, "handle": "exemptcheck"})
    assert "registration is closed" not in resp.text, (
        "the social exemption was lost — social sign-in can no longer provision an "
        "account on a closed island, which is PERMANENTLY the case in production, "
        "and reverses the 2026-06-27 decision")
    assert resp.status_code == 200, (
        f"expected the claim to succeed, got {resp.status_code}: {resp.text[:200]}")
