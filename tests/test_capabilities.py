"""``GET /capabilities`` (#4484) — the endpoint that retires a hand-written host list.

**The failure this endpoint exists to end was SILENT ON BOTH SIDES OF THE WIRE**,
and so is the way it fails: the client's parse treats a stub 200 exactly like a
404 — "unknown" — and falls back to the hardcoded allowlist without complaint. So
`200 OK` is not the property under test here. The properties are:

  1. the emitted document decodes to an EXPLICIT `true` under the client's own
     three-state parse rule (re-implemented here, not imported — the app is Dart),
     with stub/partial/hostile documents as the **must-fail arm** proving that
     assertion can tell a real answer from a shaped one;
  2. the claim is TRUE OF THE RUNNING CODE — asserted in the same test as a real
     Ed25519 round-trip through the live carriage path, so the constant cannot stay
     `true` while carriage is broken without this test going red;
  3. the document discloses nothing beyond carriage, pinned by key-set so growth on
     an unauthenticated surface has to be a decision somebody makes on purpose.
"""
from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import messages_service, signing
from aiko_gateway.domain.models import Channel, User
from aiko_gateway.main import app
from tests.test_message_signing_carriage import _origin_for


def _parse_like_the_app(doc) -> bool | None:
    """Verbatim re-implementation of `GatewayCapabilities.parse`
    (`aiko_chat_app/lib/features/chat/domain/gateway_capabilities.dart`): ONLY an
    explicit JSON boolean at `carriage.origin` is authoritative; everything else is
    `None` = unknown, which the client resolves to its allowlist seed.

    Re-implemented rather than imported because the real one is Dart. That makes it
    a transcription, and a transcription can be wrong in the same direction as the
    thing it copies — so it is exercised by the stub arm below, which is red-proven
    against THIS function, not merely asserted about it.
    """
    if not isinstance(doc, dict):
        return None
    carriage = doc.get("carriage")
    origin = carriage.get("origin") if isinstance(carriage, dict) else None
    return origin if isinstance(origin, bool) else None


@pytest_asyncio.fixture
async def client():
    # ASGITransport skips lifespan: this endpoint reads no DB and no bus.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_capabilities_is_public_and_token_less(client):
    """No Authorization header — the app fetches this on its BARE client, before
    (and independently of) any session, on every reconnect."""
    r = await client.get("/capabilities")
    assert r.status_code == 200


async def test_document_decodes_to_an_explicit_true_under_the_client_rule(client):
    r = await client.get("/capabilities")
    assert _parse_like_the_app(r.json()) is True, (
        "a document that does not decode to an explicit True leaves the client on "
        "its hardcoded allowlist, silently — the exact seven-week failure mode")


@pytest.mark.parametrize("stub, desc", [
    ({}, "empty object"),
    ({"carriage": {}}, "carriage present but empty"),
    ({"carriage": {"origin": "true"}}, "origin stringly-typed"),
    ({"carriage": {"origin": 1}}, "origin as int"),
    ({"carriage": None}, "carriage null"),
    ({"origin": True}, "origin at the top level, not under carriage"),
    ([], "not an object at all"),
])
def test_must_fail_arm_stub_documents_decode_to_unknown(stub, desc):
    """The discriminating half. Each of these is a 200 that a naive status-code
    check passes and the CLIENT reads as 'unknown' — i.e. as if the endpoint were
    still a 404. If the assertion above cannot tell these from the real document,
    it is a check whose outcome does not depend on the thing it checks."""
    assert _parse_like_the_app(stub) is None, desc


async def test_the_claim_is_true_of_the_running_carriage(client, session):
    """The binding. A real Ed25519 signature goes through the LIVE carriage path
    (validate_origin -> create_outbound -> message_view) and re-verifies from only
    the echoed data — and the endpoint's claim is asserted in the same breath.

    Removing carriage cannot leave `carriage.origin: true` standing: this test goes
    red on the round-trip half, in the file named after the claim.
    """
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()

    priv = Ed25519PrivateKey.generate()
    cmid, ts, body = "cap-roundtrip", 1720000000123, "hello world"
    raw = _origin_for(priv, channel_id=channel.id, client_msg_id=cmid,
                      signed_at_ms=ts, body=body, reply_to=None)

    origin = signing.validate_origin(raw, frame_client_msg_id=cmid)
    row, created = await messages_service.create_outbound(
        session, user=user, channel=channel, body=body, client_msg_id=cmid,
        origin=origin)
    assert created
    echoed = messages_service.message_view(row)["origin"]

    raw_pub = signing.decode_multikey(echoed["sender_pubkey"])
    rebuilt = signing.signing_bytes(
        raw_pubkey=raw_pub, channel_id=channel.id,
        client_msg_id=echoed["client_msg_id"], signed_at_ms=echoed["signed_at_ms"],
        body=body, reply_to=None)
    sig = base64.urlsafe_b64decode(echoed["sig"] + "=" * (-len(echoed["sig"]) % 4))
    Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, rebuilt)  # carriage works

    r = await client.get("/capabilities")
    assert _parse_like_the_app(r.json()) is True, (
        "the island carries origin end-to-end but advertises otherwise")


async def test_discloses_nothing_beyond_carriage(client):
    """An unauthenticated endpoint describing the island is a disclosure surface
    (`/v1/islands` taught that). Pinned by key-set so ADDING a field is a change
    somebody has to make deliberately, with the disclosure question in front of
    them — anything provenance- or person-bearing belongs on the SIGNED manifest
    (`/v1/island`), per the app tab's ADR-0008."""
    doc = (await client.get("/capabilities")).json()
    assert set(doc) == {"carriage"}
    assert set(doc["carriage"]) == {"origin"}


async def test_not_cached_across_a_deploy(client):
    """The answer changes only on deploy, and a proxy replaying the previous
    build's copy re-arms the client's allowlist fallback on an island that has
    since started carrying."""
    r = await client.get("/capabilities")
    assert r.headers["cache-control"] == "no-store"
