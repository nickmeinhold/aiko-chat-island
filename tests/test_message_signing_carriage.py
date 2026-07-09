"""Sovereign message-signing CARRIAGE (#1816) — the gateway's carrier role.

The gateway persists + echoes the client's `origin` envelope so a recipient can
reconstruct the signed bytes and verify; it never verifies itself. These tests
pin three things:

  1. `signing_bytes` reproduces the app's frozen SIGNING-SPEC golden vector EXACTLY
     — the interop anchor. If this drifts, gateway and app disagree on what was
     signed and every cross-party verification silently fails.
  2. Carriage is VERIFIER-SUFFICIENT end-to-end: a real Ed25519 signature, carried
     through validate_origin -> create_outbound -> message_view, still verifies when
     reconstructed from ONLY the echoed data. This is the property #1816 must deliver.
  3. The trust boundary is fail-closed: malformed / inconsistent envelopes are
     rejected, never persisted (RED-proven per failure mode).
"""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from aiko_gateway.domain import messages_service, signing
from aiko_gateway.domain.models import Channel, User

# -- helpers -----------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(b: bytes) -> str:
    num = int.from_bytes(b, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def _multikey(raw_pub: bytes) -> str:
    return "z" + _b58encode(b"\xed\x01" + raw_pub)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _origin_for(priv: Ed25519PrivateKey, *, channel_id: str, client_msg_id: str,
                signed_at_ms: int, body: str, reply_to: str | None) -> dict:
    raw_pub = priv.public_key().public_bytes_raw()
    sig = priv.sign(signing.signing_bytes(
        raw_pubkey=raw_pub, channel_id=channel_id, client_msg_id=client_msg_id,
        signed_at_ms=signed_at_ms, body=body, reply_to=reply_to))
    return {
        "v": 1, "alg": "EdDSA", "key_version": 1,
        "sender_pubkey": _multikey(raw_pub),
        "client_msg_id": client_msg_id,
        "signed_at_ms": signed_at_ms,
        "sig": _b64url(sig),
    }


# -- 1. golden vector (interop anchor) ---------------------------------------
def test_signing_bytes_matches_frozen_golden_vector():
    """The app's SIGNING-SPEC golden vector, byte-for-byte. A change here is a v2."""
    got = signing.signing_bytes(
        raw_pubkey=bytes(range(32)), channel_id="chan-1", client_msg_id="tmp-abc",
        signed_at_ms=1720000000000, body="hello world", reply_to=None)
    expected = (
        "0000001561696b6f636861743a6d73673a76313a4564445341"
        "00000020000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
        "000000066368616e2d3100000007746d702d616263"
        "0000019077fd3000"
        "0000000b68656c6c6f20776f726c64"
        "00000000"
    )
    assert got.hex() == expected


def test_reply_to_none_and_empty_string_sign_identically():
    """reply_to absent must equal reply_to='' — the spec pins the empty-string encoding."""
    kw = dict(raw_pubkey=bytes(range(32)), channel_id="c", client_msg_id="m",
              signed_at_ms=1, body="b")
    assert signing.signing_bytes(**kw, reply_to=None) == signing.signing_bytes(**kw, reply_to="")


# -- 2. carriage is verifier-sufficient end-to-end ---------------------------
@pytest.mark.asyncio
async def test_carriage_roundtrip_is_verifier_sufficient(session):
    """A real signature survives validate_origin -> create_outbound -> message_view,
    and re-verifies from ONLY the echoed data (message_view fields + origin)."""
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()

    priv = Ed25519PrivateKey.generate()
    cmid, ts, body = "tmp-xyz", 1720000000123, "hello world"
    raw_origin = _origin_for(priv, channel_id=channel.id, client_msg_id=cmid,
                             signed_at_ms=ts, body=body, reply_to=None)

    # trust-boundary validation accepts a well-formed envelope, unchanged
    origin = signing.validate_origin(raw_origin, frame_client_msg_id=cmid)
    assert origin == raw_origin

    row, created = await messages_service.create_outbound(
        session, user=user, channel=channel, body=body, client_msg_id=cmid, origin=origin)
    assert created
    view = messages_service.message_view(row)

    # Reconstruct signingBytes from ONLY what a recipient receives: message_view's
    # channel_id/body/reply_to + the echoed origin's pubkey/client_msg_id/signed_at_ms.
    echoed = view["origin"]
    raw_pub = signing.decode_multikey(echoed["sender_pubkey"])
    rebuilt = signing.signing_bytes(
        raw_pubkey=raw_pub, channel_id=view["channel_id"],
        client_msg_id=echoed["client_msg_id"], signed_at_ms=echoed["signed_at_ms"],
        body=view["body"], reply_to=view["reply_to"])
    sig = base64.urlsafe_b64decode(echoed["sig"] + "=" * (-len(echoed["sig"]) % 4))
    # Does NOT raise -> the carried data is sufficient to verify the original signature.
    Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, rebuilt)


@pytest.mark.asyncio
async def test_message_view_omits_origin_when_unsigned(session):
    """Unsigned + bus-born messages carry no origin key at all (absent == unverified)."""
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()
    row, _ = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi", client_msg_id="m1", origin=None)
    assert "origin" not in messages_service.message_view(row)


# -- 3. trust boundary is fail-closed ----------------------------------------
def _valid_raw():
    priv = Ed25519PrivateKey.generate()
    return _origin_for(priv, channel_id="c", client_msg_id="m1",
                       signed_at_ms=1720000000000, body="b", reply_to=None)


def test_validate_origin_none_is_legal():
    assert signing.validate_origin(None, frame_client_msg_id="m1") is None


@pytest.mark.parametrize("mutate,desc", [
    (lambda o: o.update(alg="none"), "alg not allowlisted (alg-confusion)"),
    (lambda o: o.update(alg="RS256"), "alg swapped"),
    (lambda o: o.update(v=2), "unsupported version"),
    (lambda o: o.update(key_version=0), "key_version < 1"),
    (lambda o: o.update(key_version=True), "key_version bool sneaking as int"),
    (lambda o: o.update(signed_at_ms=-1), "negative signed_at_ms"),
    (lambda o: o.update(signed_at_ms="123"), "signed_at_ms not int"),
    (lambda o: o.update(sender_pubkey="Qm-not-multibase"), "pubkey not z-multibase"),
    (lambda o: o.update(sender_pubkey="z" + "1" * 40), "pubkey bad multicodec/len"),
    (lambda o: o.update(sig="!!!!"), "sig not base64url"),
    (lambda o: o.update(sig=_b64url(b"\x00" * 63)), "sig wrong length (63)"),
    (lambda o: o.pop("sig"), "missing required key"),
    (lambda o: o.update(extra="x"), "unexpected extra key"),
])
def test_validate_origin_rejects_malformed(mutate, desc):
    raw = _valid_raw()
    mutate(raw)
    with pytest.raises(signing.OriginError):
        signing.validate_origin(raw, frame_client_msg_id="m1")


def test_validate_origin_rejects_client_msg_id_mismatch():
    """origin.client_msg_id MUST equal the frame's — the signed id is the stored id."""
    raw = _valid_raw()  # signed with client_msg_id="m1"
    with pytest.raises(signing.OriginError):
        signing.validate_origin(raw, frame_client_msg_id="DIFFERENT")
