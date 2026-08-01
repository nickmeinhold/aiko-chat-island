"""Island signed self-manifest (crucible-09 Phase A, A1) — identity + endpoint tests.

The trust-boundary guarantees pinned here:
  * a manifest built by the island VERIFIES against its own declared public key;
  * ANY tamper of a signed field (mode, base_url, id, …) makes verification FAIL —
    the whole self-entry has provenance, not just `mode`;
  * the island Multikey is the canonical `z6Mk…` ed25519 shape and round-trips
    through the EXISTING message-signer decoder (a cross-implementation check, not a
    self-inverse — dodging self-referential-test blindness);
  * the seed decoder fails closed on a wrong-length / mis-encoded key;
  * `GET /v1/island` serves a manifest whose signature verifies.
"""
from __future__ import annotations

import base64
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import island_identity as ii
from aiko_gateway.domain import signing
from aiko_gateway.main import app

# A fixed non-dev seed for deterministic unit tests (32 bytes, base64url unpadded).
_TEST_SEED = base64.urlsafe_b64encode(b"unit-test-island-seed-32-bytes!!").rstrip(b"=").decode()

_FIELDS = dict(
    id="test-island",
    display_name="Test Island",
    base_url="https://test.example",
    mode="moderator",
    key_version=1,
    # A fixed signed_at_ms so unit builds are deterministic (the endpoint stamps
    # `now`; the codec takes it as an explicit field — see the freshness tests below).
    signed_at_ms=1720000000000,
)


def _manifest(**over) -> dict:
    return ii.build_signed_manifest(**{**_FIELDS, **over, "seed_b64url": _TEST_SEED})


# --- signature correctness + tamper detection -------------------------------- #

def test_manifest_verifies():
    assert ii.verify_manifest(_manifest()) is True


def test_manifest_has_expected_shape():
    m = _manifest()
    assert m["v"] == ii.V and m["alg"] == ii.ALG
    assert set(m) == {"v", "alg", "id", "display_name", "base_url", "mode",
                      "key_version", "signed_at_ms", "island_pubkey", "signature"}
    assert m["island_pubkey"].startswith("z6Mk")  # canonical ed25519 Multikey prefix


@pytest.mark.parametrize("field,bad", [
    ("mode", "e2ee"),                       # the mislabel we most care about
    ("base_url", "https://evil.example"),   # the field the design almost left unsigned
    ("id", "other-island"),
    ("display_name", "Impersonator"),
    ("key_version", 2),
    ("signed_at_ms", 1720000000001),  # freshness IS signed — a replay can't re-date it
])
def test_tamper_of_any_signed_field_fails_verification(field, bad):
    m = _manifest()
    m[field] = bad
    assert ii.verify_manifest(m) is False


def test_tampered_signature_fails():
    m = _manifest()
    # Flip the signature to a valid-length but wrong value (all-zero 64 bytes).
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 64).rstrip(b"=").decode()
    assert ii.verify_manifest(m) is False


def test_signature_from_a_different_key_fails():
    # A manifest whose FIELDS say key A but whose pubkey/signature are key B's is a
    # forgery attempt; verify must reject it. Build with a second seed, then splice
    # the first manifest's pubkey in.
    other_seed = base64.urlsafe_b64encode(b"a-different-island-seed-32-byte!").rstrip(b"=").decode()
    m = _manifest()
    m["island_pubkey"] = ii.public_multikey(ii.private_key_from_seed(other_seed))
    assert ii.verify_manifest(m) is False


def test_malformed_manifest_raises_not_returns_false():
    # A structurally-unusable manifest (missing a field) is distinguishable from a
    # verified-false one: it RAISES, so a caller can tell "not a manifest" from "bad sig".
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest({"island_pubkey": "znope", "signature": "x"})


# --- Multikey codec: cross-implementation round-trip ------------------------- #

def test_pubkey_roundtrips_through_the_message_signer_decoder():
    # encode_multikey (new) -> decode_multikey (existing, prod-exercised) == identity.
    # Uses the INDEPENDENT decoder as the oracle, not encode's own inverse.
    priv = ii.private_key_from_seed(_TEST_SEED)
    raw = priv.public_key().public_bytes_raw()
    mk = signing.encode_multikey(raw)
    assert signing.decode_multikey(mk) == raw


def test_encode_multikey_rejects_wrong_length():
    with pytest.raises(signing.OriginError):
        signing.encode_multikey(b"\x00" * 31)


# --- seed decoding: fail closed ---------------------------------------------- #

def test_decode_seed_rejects_wrong_length():
    short = base64.urlsafe_b64encode(b"too-short").rstrip(b"=").decode()
    with pytest.raises(ii.IslandIdentityError):
        ii.decode_seed(short)


def test_decode_seed_rejects_non_base64url():
    with pytest.raises(ii.IslandIdentityError):
        ii.decode_seed("not base64url! has spaces and =")


def test_decode_seed_accepts_valid():
    assert len(ii.decode_seed(_TEST_SEED)) == ii.SEED_LEN


# --- domain separation ------------------------------------------------------- #

def test_island_domain_tag_differs_from_message_signer():
    # An island-manifest signature must never be a valid message signature — the
    # domain tags being distinct is what guarantees it at the byte level.
    assert ii.DOMAIN_TAG != signing.DOMAIN_TAG
    assert ii.signing_bytes(**_FIELDS).startswith(
        __import__("struct").pack(">I", len(ii.DOMAIN_TAG)) + ii.DOMAIN_TAG.encode())


# --- the endpoint ------------------------------------------------------------ #

@pytest_asyncio.fixture
async def client():
    # ASGITransport does not trigger lifespan, so the aiko bus never starts and no DB
    # is needed — /v1/island reads only config + the process directory singleton.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_island_returns_a_verifying_signed_manifest(client):
    r = await client.get("/v1/island")
    assert r.status_code == 200
    m = r.json()
    # The test harness runs on the config defaults: mode=moderator, dev seed.
    assert m["mode"] == "moderator"
    assert m["island_pubkey"].startswith("z6Mk")
    assert ii.verify_manifest(m) is True


async def test_get_island_is_public_no_auth(client):
    # Identity + mode are public discovery info (like /v1/islands) — no auth header.
    r = await client.get("/v1/island")
    assert r.status_code == 200


async def test_get_island_503_when_no_self_identity(client, monkeypatch):
    # No valid self identity configured → there is nothing authentic to sign, so the
    # endpoint fails closed with 503 rather than emitting an unsigned/partial manifest.
    from aiko_gateway.domain.peers_service import directory
    monkeypatch.setattr(directory, "_self", None)
    r = await client.get("/v1/island")
    assert r.status_code == 503


# --- verify is the strict mirror of validate_origin (fail-closed) ------------ #

def test_verify_rejects_bad_alg_before_crypto():
    # A forged manifest can carry a VALID signature over the identity tuple while
    # advertising alg="none" (v/alg are outside the signed bytes) — the JWT
    # alg-confusion class. Verify must refuse it, not return True/False from crypto.
    m = _manifest()
    m["alg"] = "none"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_bad_v():
    m = _manifest()
    m["v"] = 999
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_extra_and_missing_keys():
    m = _manifest()
    m["surprise"] = "x"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)
    m2 = _manifest()
    del m2["base_url"]
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m2)


def test_verify_rejects_wrong_length_signature():
    # A 32-byte (not 64) signature must be a structural reject, not an uncaught
    # ValueError out of Ed25519.verify.
    m = _manifest()
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode()
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_padded_signature():
    # Permissive base64 would accept '='-padded / standard-alphabet; the strict
    # decoder must not.
    m = _manifest()
    m["signature"] = base64.urlsafe_b64encode(b"\x00" * 64).decode()  # keeps '=' padding
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_bool_and_oob_key_version():
    # True is an int subclass that would pack as 1; an out-of-range int would raise
    # struct.error. Both must be structural rejects.
    for bad in (True, -1, 2**32):
        m = _manifest()
        m["key_version"] = bad
        with pytest.raises(ii.IslandIdentityError):
            ii.verify_manifest(m)


def test_verify_rejects_bad_mode():
    m = _manifest()
    m["mode"] = "plaintext-lol"
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_build_rejects_bad_mode_and_key_version():
    # The signing door itself refuses to mint a manifest for an unknown mode or an
    # out-of-range key_version — a Phase B helper can't sign unvalidated vocabulary.
    with pytest.raises(ii.IslandIdentityError):
        _manifest(mode="bogus")
    with pytest.raises(ii.IslandIdentityError):
        _manifest(key_version=2**32)


def test_verify_rejects_bool_v():
    # True == 1 would satisfy a bare `!= V` check; v must be a real int (the same
    # bool-exclusion validate_origin applies to its discriminators).
    m = _manifest()
    m["v"] = True
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_verify_rejects_oversized_fields():
    # The A4 peer door caps untrusted strings before crypto — an overlong base_url
    # must be a structural reject, not fed into signing_bytes.
    m = _manifest()
    m["base_url"] = "https://" + "a" * 300
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_valid_modes_is_the_single_enum_vocabulary():
    # VALID_MODES is DERIVED from IslandMode — one SoT shared with config, no drift.
    from aiko_gateway.domain.island_mode import IslandMode
    assert ii.VALID_MODES == frozenset(IslandMode)
    assert "moderator" in ii.VALID_MODES  # StrEnum members compare by value


# --- base64url canonicalization (dev-seed alias bypass, Carnot HIGH) --------- #

def test_b64url_raw_rejects_noncanonical_alias():
    # `…SE` and `…SF` decode to the SAME 32 bytes; only the canonical spelling is
    # accepted. Without this, a non-canonical alias of the dev seed would decode to
    # the dev KEY and slip past a string-equality prod guard.
    canonical = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    raw = signing.b64url_raw(canonical, expect_len=32, field="t")
    assert len(raw) == 32
    alias = canonical[:-1] + ("F" if canonical[-1] != "F" else "G")
    # The alias decodes to the same bytes (malleable) but is non-canonical → rejected.
    if base64.urlsafe_b64decode(alias + "=") == raw:
        with pytest.raises(signing.OriginError):
            signing.b64url_raw(alias, expect_len=32, field="t")


def test_b64url_raw_rejects_overlong():
    with pytest.raises(signing.OriginError):
        signing.b64url_raw("a" * 500, expect_len=32, field="t")


def test_verify_rejects_empty_identity_fields():
    # A hollow id/base_url is a vacuous navigation target — reject at the codec so the
    # A4 door can't accept one (symmetric with coerce_island's non-empty rule).
    for field in ("id", "base_url", "display_name"):
        m = _manifest()
        m[field] = ""
        with pytest.raises(ii.IslandIdentityError):
            ii.verify_manifest(m)


def test_build_rejects_empty_id():
    with pytest.raises(ii.IslandIdentityError):
        _manifest(id="")


async def test_get_island_sets_no_store(client):
    r = await client.get("/v1/island")
    assert r.headers.get("cache-control") == "no-store"


# --- signed_at_ms freshness binding (#2452, A4 anti-replay prerequisite) ------ #
#
# The manifest signature is ETERNAL over the identity tuple: an old signed
# `moderator` manifest verifies forever under the same key even after the operator
# flips mode. Phase A's honest self-fetch over TLS has no replay surface, but A4's
# peer federation handshake does. Binding a signed_at_ms into the bytes gives the A4
# door a recency lever (is_fresh); the per-request signing in rest/island.py makes
# the honest path always-fresh for free. (Strict epoch monotonicity is a clean
# future v3 when A4's per-peer high-water store lands — see the task.)

def test_manifest_is_v2_envelope():
    # Adding a field to the signed bytes is a v2 envelope, never a silent add — the
    # module's own versioning discipline. A verifier pinning v==2 rejects any v1.
    assert ii.V == 2
    assert _manifest()["v"] == 2


def test_v1_shaped_manifest_is_rejected():
    # A pre-freshness (v1) manifest — no signed_at_ms, v=1 — must not verify against a
    # v2 verifier: it fails BOTH the frozen key-set check and the pinned v check, so a
    # downgrade to the eternal-signature format is a structural reject, not a fallback.
    m = _manifest()
    del m["signed_at_ms"]
    m["v"] = 1
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_signed_at_ms_in_the_domain_separated_bytes():
    # The freshness field is length-agnostic (fixed u64), so prove it changes the
    # signed bytes: two manifests differing ONLY in signed_at_ms sign different bytes.
    a = ii.signing_bytes(**_FIELDS)
    b = ii.signing_bytes(**{**_FIELDS, "signed_at_ms": _FIELDS["signed_at_ms"] + 1})
    assert a != b


@pytest.mark.parametrize("bad", [
    True,            # bool is an int subclass — must not pack as 1
    -1,              # negative time is nonsense
    "1720000000000", # a string is not an int
    2**63,           # past the sane u64-ish upper bound
    1.5,             # a float is not an int
])
def test_verify_rejects_bad_signed_at_ms(bad):
    m = _manifest()
    m["signed_at_ms"] = bad
    with pytest.raises(ii.IslandIdentityError):
        ii.verify_manifest(m)


def test_build_rejects_bad_signed_at_ms():
    # The signing door itself refuses to mint a manifest with a nonsense timestamp —
    # symmetric with the mode/key_version build guards.
    with pytest.raises(ii.IslandIdentityError):
        _manifest(signed_at_ms=-1)
    with pytest.raises(ii.IslandIdentityError):
        _manifest(signed_at_ms=True)


def test_signed_at_ms_cap_mirrors_the_message_signer():
    # One cap across both Ed25519 signers (a rename can't silently weaken a shared
    # trust-boundary bound — the MAX_PUBKEY_STR precedent).
    assert ii.MAX_SIGNED_AT_MS == signing.MAX_SIGNED_AT_MS


# --- is_fresh: a PURE recency bound on the timestamp int (pure; caller supplies now) - #
#
# is_fresh takes the signed_at_ms INTEGER, not the manifest (PR#108 cage-match, Carnot
# + Tesla): a recency check that never sees a manifest cannot be mistaken for a trust
# gate nor bless a non-manifest. verify-THEN-fresh composition is the A4 door's job
# (task #12). max_age_ms is REQUIRED (no silent default window); skew_ms defaults to 0.

def test_is_fresh_true_within_window():
    ts = _FIELDS["signed_at_ms"]
    # Signed 1 minute ago, window is 5 minutes → fresh.
    assert ii.is_fresh(ts, now_ms=ts + 60_000, max_age_ms=300_000) is True


def test_is_fresh_false_when_too_old():
    ts = _FIELDS["signed_at_ms"]
    # Signed 10 minutes ago, window is 5 minutes → a stale posture, rejected.
    assert ii.is_fresh(ts, now_ms=ts + 600_000, max_age_ms=300_000) is False


def test_is_fresh_false_when_too_far_future():
    ts = _FIELDS["signed_at_ms"]
    # Signed 10 minutes in the FUTURE, beyond clock skew → rejected (a stamp can't
    # legitimately predate the verifier's clock by more than skew).
    assert ii.is_fresh(ts, now_ms=ts - 600_000, max_age_ms=300_000,
                       skew_ms=60_000) is False


def test_is_fresh_true_within_skew():
    ts = _FIELDS["signed_at_ms"]
    # A small clock skew (30s future) is tolerated within the 60s skew allowance.
    assert ii.is_fresh(ts, now_ms=ts - 30_000, max_age_ms=300_000,
                       skew_ms=60_000) is True


def test_is_fresh_no_skew_grace_by_default():
    # skew_ms defaults to 0 (fail-closed): a stamp even 1ms in the future is rejected
    # unless the caller explicitly grants skew grace.
    ts = _FIELDS["signed_at_ms"]
    assert ii.is_fresh(ts, now_ms=ts - 1, max_age_ms=300_000) is False
    assert ii.is_fresh(ts, now_ms=ts, max_age_ms=300_000) is True


def test_is_fresh_will_not_bless_a_manifest_dict():
    # THE adversarial composition case (Carnot REQUEST_CHANGES + Tesla, PR#108): the
    # old is_fresh(manifest) returned True for a naked {"signed_at_ms": now} dict — a
    # "fresh" verdict on forgery-shaped garbage that had never been signature-verified.
    # The int-only signature makes that structurally impossible: a dict is not an int,
    # so it fails the fail-closed type gate and RAISES rather than blessing garbage.
    ts = _FIELDS["signed_at_ms"]
    for garbage in ({"signed_at_ms": ts}, _manifest(), [ts], None):
        with pytest.raises(ii.IslandIdentityError):
            ii.is_fresh(garbage, now_ms=ts, max_age_ms=300_000)


def test_is_fresh_boundaries_are_inclusive():
    # Inclusive endpoints are load-bearing for federation interop — an off-by-one
    # between two islands is a silent freshness-flake class (Tesla, PR#108). Pin both.
    ts = _FIELDS["signed_at_ms"]
    # age == max_age_ms exactly → still fresh (upper bound inclusive).
    assert ii.is_fresh(ts, now_ms=ts + 300_000, max_age_ms=300_000) is True
    assert ii.is_fresh(ts, now_ms=ts + 300_001, max_age_ms=300_000) is False
    # age == -skew_ms exactly → still fresh (lower/future bound inclusive).
    assert ii.is_fresh(ts, now_ms=ts - 60_000, max_age_ms=300_000, skew_ms=60_000) is True
    assert ii.is_fresh(ts, now_ms=ts - 60_001, max_age_ms=300_000, skew_ms=60_000) is False


@pytest.mark.parametrize("kw", [
    {"signed_at_ms": -1},        # negative time is nonsense
    {"signed_at_ms": True},      # bool is an int subclass — must not pass
    {"signed_at_ms": 1.5},       # a float is not an int
    {"now_ms": -1},
    {"now_ms": True},
    {"now_ms": 1.5},
    {"max_age_ms": -1},          # would reject every honest peer — must fail closed
    {"max_age_ms": True},        # a 1ms window smuggled in as a bool
    {"skew_ms": -1},             # inverts the lower bound — must fail closed
    {"skew_ms": "60000"},
])
def test_is_fresh_rejects_bad_inputs(kw):
    # A trust input must not be assemblable from a typo: a bad int RAISES at the door
    # rather than silently inverting/disabling the replay boundary (Carnot
    # REQUEST_CHANGES + Tesla, PR#108). Same fail-closed discipline as signed_at_ms.
    ts = _FIELDS["signed_at_ms"]
    base = {"signed_at_ms": ts, "now_ms": ts, "max_age_ms": 300_000, "skew_ms": 60_000}
    base.update(kw)
    sig = base.pop("signed_at_ms")
    with pytest.raises(ii.IslandIdentityError):
        ii.is_fresh(sig, **base)


async def test_get_island_manifest_is_freshly_stamped(client):
    # The endpoint's "always-fresh for free" claim, pinned (Tesla, PR#108): the served
    # manifest carries a live now stamp that verifies AND passes is_fresh against real
    # wall-clock time — and a later memoize/cache regression that froze the stamp would
    # break THIS test, not silently hand A4 a dead clock.
    r = await client.get("/v1/island")
    m = r.json()
    now = int(time.time() * 1000)
    assert ii.verify_manifest(m) is True                 # verify FIRST (the real gate)
    assert abs(now - m["signed_at_ms"]) < 5_000          # stamped within ~5s of now
    # ...THEN freshness on the extracted timestamp (the verify-then-fresh order the A4
    # door will formalize, #12). A 5-minute window with 1-minute skew grace.
    assert ii.is_fresh(m["signed_at_ms"], now_ms=now,
                       max_age_ms=ii.DEFAULT_MAX_AGE_MS,
                       skew_ms=ii.DEFAULT_CLOCK_SKEW_MS) is True
    # And the anti-replay lever bites: the same stamp a window later is stale.
    assert ii.is_fresh(m["signed_at_ms"], now_ms=now + 10 * 60 * 1000,
                       max_age_ms=ii.DEFAULT_MAX_AGE_MS) is False
