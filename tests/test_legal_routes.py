"""Public legal-document routes — Privacy Policy + Terms of Use (#13).

Drives the REAL production app over its public ASGI surface (the
test_main_routes.py pattern). Two guarantees:

  * /privacy and /terms are PUBLIC — they must answer 200 with NO auth, because
    a store reviewer and a pre-login user both need to read them. (The data
    routes are auth-gated; these carry no user data.)
  * the documents still contain the compliance-critical clauses — a guard so a
    future edit can't silently strip the zero-tolerance statement or the honest
    "not end-to-end encrypted" disclosure that the App Privacy / Data Safety
    declarations are matched against.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiko_gateway.main import app


@pytest_asyncio.fixture
async def client():
    # Lifespan is NOT triggered by ASGITransport, so the aiko bus never starts;
    # these routes need no DB session either.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_privacy_is_public_html(client):
    r = await client.get("/privacy")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Privacy Policy" in r.text
    # Honest disclosure that the App Privacy / Data Safety forms depend on.
    assert "not end-to-end encrypted" in r.text.lower()
    # The absence list that backs "we collect no analytics / ads".
    assert "advertising" in r.text.lower()


async def test_terms_is_public_html(client):
    r = await client.get("/terms")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # The Apple 1.2 / Google UGC clauses must survive any future edit.
    assert "no tolerance for objectionable content" in r.text.lower()
    assert "24 hours" in r.text
    assert "block" in r.text.lower()
    assert "report" in r.text.lower()


async def test_terms_and_privacy_cross_link(client):
    # The two documents reference each other, so neither is a dead end.
    privacy = (await client.get("/privacy")).text
    terms = (await client.get("/terms")).text
    assert "/terms" in privacy
    assert "/privacy" in terms
