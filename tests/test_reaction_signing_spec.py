"""Reaction signed-bytes interop anchor (#2634 rebuild — signed from day one).

The gateway is a CARRIER, not a verifier: ``reaction_signing_bytes`` is NOT on the
production carry path. It is the exact reconstruction a verifier uses, pinned here
so the gateway's notion of "what was signed" can never silently drift from the app
signer's. This is the drift-guard half of the co-authored interop contract in
``docs/crucible/sovereign-reaction-signing/SIGNING-SPEC.md``.

STATUS: the golden vector below is the gateway's PROPOSED layout. When the app tab
confirms its real signer reproduces these bytes, this vector becomes authoritative
and a change to it is a v2 (never a silent edit) — identical discipline to the
message golden vector in ``test_message_signing_carriage``.

Built from ``signing`` alone (no ``main`` import) to keep the suite's
"never import aiko_services" isolation invariant.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from aiko_gateway.domain import signing


# -- 1. golden vector (interop anchor) ---------------------------------------
def test_reaction_signing_bytes_matches_proposed_golden_vector():
    """The proposed reaction golden vector, byte-for-byte. Once the app tab
    confirms its signer reproduces these bytes, a change here is a v2."""
    got = signing.reaction_signing_bytes(
        raw_pubkey=bytes(range(32)), channel_id="chan-1", client_msg_id="rxn-abc",
        signed_at_ms=1720000000000, target_msg_id="msg-xyz", emoji="👍", action="add")
    expected = (
        "0000001761696b6f636861743a72656163743a76313a4564445341"  # #1 domain tag
        "00000020000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"  # #2 pubkey
        "000000066368616e2d31"          # #3 channel_id 'chan-1'
        "0000000772786e2d616263"        # #4 client_msg_id 'rxn-abc'
        "0000019077fd3000"              # #5 signed_at_ms u64
        "000000076d73672d78797a"        # #6 target_msg_id 'msg-xyz'
        "00000004f09f918d"              # #7 emoji U+1F44D
        "00000003616464"                # #8 action 'add'
    )
    assert got.hex() == expected


# -- 2. field-boundary safety (length-prefixing is load-bearing) -------------
def test_length_prefixing_prevents_content_boundary_ambiguity():
    """Without length prefixes, (emoji='👍', action='add') and a shifted split
    could sign identical bytes. The u32 prefixes make each field unambiguous."""
    base = dict(raw_pubkey=bytes(range(32)), channel_id="c", client_msg_id="m",
                signed_at_ms=1, target_msg_id="t")
    a = signing.reaction_signing_bytes(**base, emoji="👍a", action="dd")
    b = signing.reaction_signing_bytes(**base, emoji="👍", action="add")
    assert a != b


def test_channel_and_target_boundary_is_unambiguous():
    base = dict(raw_pubkey=bytes(range(32)), client_msg_id="m", signed_at_ms=1,
                emoji="👍", action="add")
    a = signing.reaction_signing_bytes(**base, channel_id="ab", target_msg_id="c")
    b = signing.reaction_signing_bytes(**base, channel_id="a", target_msg_id="bc")
    assert a != b


# -- 3. action is signed → add and remove are distinct events ----------------
def test_add_and_remove_sign_different_bytes():
    """``action`` is inside the signed bytes, so a signed remove is its own
    non-repudiable event — not an unsigned retraction of a signed add."""
    base = dict(raw_pubkey=bytes(range(32)), channel_id="c", client_msg_id="m",
                signed_at_ms=1, target_msg_id="t", emoji="👍")
    assert (signing.reaction_signing_bytes(**base, action="add")
            != signing.reaction_signing_bytes(**base, action="remove"))


# -- 4. domain separation (the security reason for a distinct tag) -----------
def test_reaction_and_message_bytes_never_collide():
    """A reaction and a message over the 'same' logical inputs must produce
    different bytes — the distinct domain tag guarantees no cross-event replay."""
    raw = bytes(range(32))
    msg_bytes = signing.signing_bytes(
        raw_pubkey=raw, channel_id="c", client_msg_id="m", signed_at_ms=1,
        body="👍", reply_to="msg-xyz")
    rxn_bytes = signing.reaction_signing_bytes(
        raw_pubkey=raw, channel_id="c", client_msg_id="m", signed_at_ms=1,
        target_msg_id="msg-xyz", emoji="👍", action="add")
    assert msg_bytes != rxn_bytes
    # The separation is rooted in the domain tag (field #1: u32-len ‖ tag bytes),
    # not incidental content — the tag bytes themselves differ.
    assert signing.DOMAIN_TAG != signing.REACT_DOMAIN_TAG
    assert signing.REACT_DOMAIN_TAG.encode() in rxn_bytes
    assert signing.REACT_DOMAIN_TAG.encode() not in msg_bytes


def test_message_signature_does_not_verify_as_reaction():
    """The concrete attack the domain tag defends: a real Ed25519 signature over
    MESSAGE bytes must NOT verify against the reaction reconstruction of the same
    logical fields. Different signed bytes → verification fails."""
    priv = Ed25519PrivateKey.generate()
    pub: Ed25519PublicKey = priv.public_key()
    raw = pub.public_bytes_raw()

    msg_sig = priv.sign(signing.signing_bytes(
        raw_pubkey=raw, channel_id="c", client_msg_id="m", signed_at_ms=1,
        body="👍", reply_to="msg-xyz"))

    reaction_bytes = signing.reaction_signing_bytes(
        raw_pubkey=raw, channel_id="c", client_msg_id="m", signed_at_ms=1,
        target_msg_id="msg-xyz", emoji="👍", action="add")

    import pytest
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        pub.verify(msg_sig, reaction_bytes)


# -- 5. round-trip: a real signature reconstructs + verifies from echoed data -
def test_reaction_signature_verifies_from_reconstructed_bytes():
    """VERIFIER-SUFFICIENT: sign real reaction bytes, then reconstruct them from
    only the data a verifier would have (echoed origin + frame fields) and verify.
    This is the property the co-authored spec must deliver end to end."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw = pub.public_bytes_raw()

    signed_bytes = signing.reaction_signing_bytes(
        raw_pubkey=raw, channel_id="chan-1", client_msg_id="rxn-abc",
        signed_at_ms=1720000000000, target_msg_id="msg-xyz", emoji="👍", action="add")
    sig = priv.sign(signed_bytes)

    # A verifier rebuilds the bytes from echoed fields and the wire pubkey.
    rebuilt = signing.reaction_signing_bytes(
        raw_pubkey=raw, channel_id="chan-1", client_msg_id="rxn-abc",
        signed_at_ms=1720000000000, target_msg_id="msg-xyz", emoji="👍", action="add")
    pub.verify(sig, rebuilt)  # raises InvalidSignature on drift; passing == conformant
