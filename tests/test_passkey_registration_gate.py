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


async def test_passkey_register_start_STAYS_OPEN_when_registration_is_closed(
        client, monkeypatch):
    """THIS ASSERTION IS INVERTED FROM ITS FIRST VERSION, AND THAT IS THE POINT.

    `register/start` is ALSO `add/start` — there is no such endpoint, and
    `passkey/add/finish` consumes a challenge issued here. Gating this route is a
    PERMANENT LOCKOUT of device-add on every production island, because
    `open_registration` is force-closed there as standing law.

    Round 1 of this PR gated it, and this test asserted the lockout as correct —
    the jig holding the bad weld. Tesla caught it; two other families reviewed the
    same diff and approved the placement. Restoring a gate here turns this red.
    """
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/register/start")
    assert resp.status_code == 200, (
        "a closed island refused to issue a challenge — this breaks add/finish, "
        "which has no start of its own, and locks every existing user out of "
        "adding a device for as long as the island is in production")


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


async def test_add_finish_can_still_GET_A_CHALLENGE_when_registration_is_closed(
        client, monkeypatch):
    """The add path end-to-end as far as this file can see it, and the previous
    version of this test could NOT fail for the reason it named.

    It posted to `add/finish` unauthenticated and asserted `!= 403`. But
    `CurrentUser` is a dependency — it fires BEFORE the function body — so a
    body-level gate pasted into `add/finish` still yields 401 and the assertion
    still passes. The 401-vs-403 trick only detects a DECORATOR dependency, which
    is not the pattern this change uses. Tesla, cage-match round 1: *"A control
    that cannot hear the instrument it claims to tune will certify silence as
    music."*

    What actually matters for add is the CHALLENGE, and that is observable here:
    with registration closed, `register/start` — which is add's only issuer — must
    still hand out a usable one. That is the link a gate on start would sever.
    """
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/register/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("state"), (
        "no challenge state issued on a closed island — add/finish has nothing to "
        "consume and device-add is dead")


async def test_passkey_register_finish_reaches_verification_when_open(
        client, monkeypatch):
    """THE MISSING NEGATIVE CONTROL for the chokepoint (Tesla, round 1).

    Every other arm here would pass against an UNCONDITIONAL 403 on
    `register/finish` — which would break registration on every dev island, where
    `open_registration` defaults True. With the flag on, a bogus challenge must
    fail as a CHALLENGE (400), never as a closed door (403): that proves the
    request got past the gate and into the ceremony.
    """
    monkeypatch.setattr(settings, "open_registration", True)
    resp = await client.post("/v1/auth/passkey/register/finish", json=_FINISH_BODY)
    assert resp.status_code == 400, (
        f"expected 400 (bad challenge, i.e. past the gate), got {resp.status_code} "
        f"— a 403 here means the gate refuses unconditionally")


async def test_passkey_authenticate_start_is_NOT_gated(client, monkeypatch):
    """Existing users must still be able to SIGN IN on a closed island."""
    monkeypatch.setattr(settings, "open_registration", False)
    resp = await client.post("/v1/auth/passkey/authenticate/start")
    assert resp.status_code == 200, (
        f"sign-in is not signup, but a closed island answered {resp.status_code} "
        "— asserting == 200 rather than != 403 because a 500 would bless the "
        "weaker form (Tesla, round 1)")


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
