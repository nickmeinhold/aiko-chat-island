"""Fail-closed configuration guards (prod auth hardening).

Invariants (all prod-only unless noted):
  1. A production-like deployment MUST NOT boot with the dev-default/weak
     jwt_secret — `Settings()` raises rather than serving forgeable tokens.
  2. Open self-registration defaults OFF in production and ON in dev, with an
     explicit override either way (and no prod break-glass until I2 lands).
  3. Social sign-in enabled in prod requires a usable provider (native client
     IDs or a fully-configured broker); an empty allowlist would reject-all.
  4. An OAuth broker provider is both-or-neither: a half (id XOR secret) config
     refuses boot rather than failing opaquely mid-login.
  5. At least one viable NEW-USER ingress (passkey ∨ social) must exist in prod —
     otherwise the island can log in existing accounts but can never onboard
     anyone (a locked, un-joinable deployment). #1927/#49.
  6. If passkey is enabled in prod, passkey_rp_id gets a BEST-EFFORT sanity check
     against the configured host (must be multi-label AND equal/registrable-parent
     per the WebAuthn rp_id rule). This catches an obviously-wrong rp_id but cannot
     prove a working ceremony — Settings can't see the real serving host (cage-match
     PR#97). Invariant 5 (an ingress is ENABLED) is the complete guarantee.

`_env_file=None` disables the repo `.env` so these tests exercise the code
defaults, not whatever a local `.env` happens to set.
"""
from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from aiko_gateway.config import _DEV_ISLAND_SEED, _DEV_JWT_SECRET, Settings

# A real (non-dev) island signing seed for prod-boot tests: 32 bytes, base64url.
_REAL_ISLAND_SEED = base64.urlsafe_b64encode(
    b"prod-island-seed-32-bytes-long!!").rstrip(b"=").decode()


# --- invariant 1: fail-closed jwt_secret ------------------------------------

def test_prod_with_default_secret_raises():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_DEV_JWT_SECRET)


_STRONG_SECRET = "a-real-32-byte-minimum-secret-value"  # 35 chars >= 32


def test_prod_with_real_secret_boots():
    # passkey_enabled makes this a joinable island — invariant 5 (below) now
    # requires a viable ingress in prod, so a password-only prod is a locked
    # island. This test is about the jwt_secret invariant, so give it an ingress.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True)
    assert s.is_production is True


def test_prod_with_empty_secret_raises():
    # A denylist on the dev default alone is a sieve — an empty secret must also
    # be rejected in prod (it would sign trivially-forgeable tokens).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret="   ")


def test_prod_with_short_secret_raises():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret="too-short")


def test_dev_with_default_secret_boots():
    # The whole point of the dev default: local dev must stay frictionless.
    s = Settings(_env_file=None, environment="dev", jwt_secret=_DEV_JWT_SECRET)
    assert s.is_production is False


def test_unknown_environment_is_treated_as_production():
    # Fail-closed: an unrecognized environment is production-like, so the dev
    # default secret must still be rejected.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="staging", jwt_secret=_DEV_JWT_SECRET)


def test_missing_environment_defaults_to_production(monkeypatch):
    # THE fail-closed invariant: forgetting ENVIRONMENT entirely must NOT boot
    # with the dev secret. Absence resolves to "production" (the default), so a
    # deploy that supplies real config but forgets ENVIRONMENT still crashes
    # rather than serving forgeable tokens. (conftest sets ENVIRONMENT=test for
    # the suite; clear it here to exercise true absence.)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret=_DEV_JWT_SECRET)


def test_whitespace_padded_environment_still_recognized():
    # ENVIRONMENT=" dev " must be read as dev, not mis-hardened to production.
    s = Settings(_env_file=None, environment="  dev  ", jwt_secret=_DEV_JWT_SECRET)
    assert s.is_production is False


# --- invariant 2: registration gating ---------------------------------------

def test_open_registration_defaults_on_in_dev():
    s = Settings(_env_file=None, environment="dev")
    assert s.open_registration is True


def test_open_registration_defaults_off_in_prod():
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True)
    assert s.open_registration is False


def test_open_registration_override_rejected_in_prod():
    # No break-glass while I2 (#36) is unenforced: an explicit OPEN_REGISTRATION
    # in prod fails closed rather than reopening the endpoint.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, open_registration=True)


def test_open_registration_override_allowed_in_dev():
    # Dev can still flip it either way.
    s = Settings(_env_file=None, environment="dev", open_registration=False)
    assert s.open_registration is False


# --- invariant 3: social sign-in client-ID allowlist (#13) ------------------

def test_prod_social_enabled_without_client_ids_raises():
    # Enabling social in prod with NO client IDs is a guaranteed-broken config:
    # the verifier's empty audience allowlist would reject every token. Fail LOUD
    # at boot rather than silently 401 every real login.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, social_signin_enabled=True)


def test_prod_social_enabled_with_client_ids_boots():
    # Unlike open_registration, social sign-in IS permitted in prod (the I2
    # tradeoff is accepted) — as long as at least one provider client ID is set.
    s = Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, social_signin_enabled=True,
                 google_client_ids=["my-client-id.apps.googleusercontent.com"])
    assert s.social_signin_enabled is True


def test_dev_social_enabled_without_client_ids_boots():
    # Dev stays frictionless — the boot guard is prod-only (an empty allowlist
    # still rejects tokens at runtime, but dev isn't blocked from booting).
    s = Settings(_env_file=None, environment="dev", social_signin_enabled=True)
    assert s.social_signin_enabled is True


def test_prod_social_enabled_with_only_broker_boots():
    # A broker-only deployment (cage-match #30 r2): social sign-in enabled in prod
    # with NO native client IDs but a fully-configured broker provider (github id +
    # secret) satisfies "at least one usable provider" — the broker IS the usable
    # provider, so it must boot.
    s = Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, social_signin_enabled=True,
                 github_client_id="gh-id", github_client_secret="gh-secret")
    assert s.social_signin_enabled is True


def test_prod_social_enabled_with_nothing_configured_raises():
    # Social enabled in prod with NOTHING (no native IDs, no broker) is still a
    # guaranteed-broken config — fail LOUD at boot (cage-match #30 r2).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, social_signin_enabled=True)


# --- invariant 4: OAuth broker provider XOR config (#21) --------------------

def test_prod_broker_id_without_secret_raises():
    # A half-configured broker provider (id but no secret) is a latent footgun —
    # it would fail mid-login with an opaque 4xx. Fail LOUD at boot.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, github_client_id="gh-id")


def test_prod_broker_secret_without_id_raises():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, github_client_secret="gh-secret")


def test_prod_broker_both_set_boots():
    # Broker configured but social_signin_enabled defaults False, so the broker is
    # NOT advertised (invariant 5) — passkey_enabled supplies the viable ingress so
    # this test stays about the broker XOR invariant, not onboarding.
    s = Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET, passkey_enabled=True,
                 github_client_id="gh-id", github_client_secret="gh-secret")
    assert s.github_client_id == "gh-id"


def test_prod_broker_neither_set_boots():
    # No broker provider configured at all is fine — the provider is simply absent.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True)
    assert s.github_client_id == ""


def test_dev_broker_half_config_boots():
    # Dev stays frictionless — the XOR guard is prod-only (a half-config dev
    # provider simply lists/behaves as not-configured at runtime, fail-closed).
    s = Settings(_env_file=None, environment="dev", github_client_id="gh-id")
    assert s.github_client_id == "gh-id"


# --- invariant 5: at-least-one-viable-ingress in prod (#1927 / #49) ----------

def test_prod_no_ingress_raises():
    # THE locked-island guard. passkey OFF + social OFF + open_registration
    # force-closed in prod = an island that can authenticate pre-existing accounts
    # but can never onboard a new user. Fail LOUD at boot (same fail-closed
    # discipline as the broker XOR guard). This is the durable fix for "retiring
    # social is one env line from bricking onboarding" (#1923).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET)


def test_prod_passkey_only_boots():
    # Passkey alone is a complete ingress (registration creates the account).
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, social_signin_enabled=False)
    assert s.passkey_enabled is True


def test_prod_social_only_boots():
    # Social alone (with a provider) is a complete ingress — passkey may stay dark.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=False, social_signin_enabled=True,
                 google_client_ids=["my-client-id.apps.googleusercontent.com"])
    assert s.social_signin_enabled is True


def test_prod_both_ingresses_boot():
    # The passkey-migration steady state: both on. The #1923 retirement flips
    # social off only AFTER passkey is enabled, so this guard is never tripped in a
    # correct migration — only by turning BOTH off (the bug).
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, social_signin_enabled=True,
                 google_client_ids=["my-client-id.apps.googleusercontent.com"])
    assert s.passkey_enabled and s.social_signin_enabled


def test_dev_no_ingress_boots():
    # Dev is exempt (guard is prod-only) AND has open_registration on by default,
    # so a dev island is always joinable via /register regardless of the flags.
    s = Settings(_env_file=None, environment="dev",
                 passkey_enabled=False, social_signin_enabled=False)
    assert s.is_production is False


# --- invariant 6: passkey viability — rp_id must match the serving host (PR#97) --

def test_prod_passkey_rp_id_mismatch_raises():
    # THE hollow-passkey guard (cage-match PR#97, Carnot). passkey_enabled=True with
    # an rp_id bound to a DIFFERENT host (here: a passkey-only island that forgot to
    # change passkey_rp_id off another island's default) passes the flag-only
    # viable-ingress check but can never complete a registration. Fail LOUD at boot.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.enspyr.co",
                 passkey_rp_id="chat.imagineering.cc")


def test_prod_passkey_rp_id_match_boots():
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.enspyr.co",
                 passkey_rp_id="chat.enspyr.co")
    assert s.passkey_rp_id == "chat.enspyr.co"


def test_prod_passkey_rp_id_registrable_parent_boots():
    # WebAuthn allows rp_id to be a registrable PARENT of the serving host — a
    # credential scoped to example.com is usable on chat.example.com. Must boot.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.example.com",
                 passkey_rp_id="example.com")
    assert s.passkey_enabled is True


def test_prod_passkey_rp_id_sibling_domain_raises():
    # A sibling (not a parent): rp_id=other.example.com is NOT a suffix of the host
    # chat.example.com — endswith('.'+rp) must not accept it. Fail closed.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.example.com",
                 passkey_rp_id="other.example.com")


def test_prod_passkey_single_label_rp_raises():
    # A single-label / public-suffix rp_id ("com") would pass a naive suffix check
    # (host "chat.com" endswith ".com") but no browser scopes a credential to a
    # public suffix — reject it (cage-match PR#97, Carnot). KNOWN RESIDUAL: a
    # multi-label public suffix ("co.uk") still slips through without a PSL (#51).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.com",
                 passkey_rp_id="com")


def test_prod_passkey_partial_label_suffix_raises():
    # Regression guard (cage-match PR#97 round 3, Tesla): the check is a LABEL-
    # boundary suffix, not a character suffix. rp_id="app.com" must NOT match host
    # "myapp.com" — `"myapp.com".endswith(".app.com")` is False because the "."+rp
    # construction requires a full-label boundary. Locks the refutation of the
    # claimed "myapp/app.com trap".
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://myapp.com",
                 passkey_rp_id="app.com")


def test_prod_passkey_both_defaults_boot_is_known_limitation():
    # PINS the documented blind spot (cage-match PR#97, Tesla): Settings cannot see
    # the REAL serving host, so if gateway_base_url AND passkey_rp_id are BOTH left
    # at their (matching) defaults, this boots even though a deploy on a DIFFERENT
    # host would have a hollow passkey ingress. This is NOT a bug we can fix at the
    # Settings layer — it's asserted here so the limitation is explicit and any
    # future change to the default-matching behavior is a conscious one. Full
    # passkey-config correctness belongs at deploy/runtime (#51).
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True)  # base_url + rp_id both default to chat.imagineering.cc
    assert s.passkey_enabled is True


def test_prod_passkey_disabled_skips_rp_id_check():
    # Passkey off → the rp_id check is irrelevant; social carries the ingress.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=False, social_signin_enabled=True,
                 gateway_base_url="https://chat.enspyr.co",
                 passkey_rp_id="chat.imagineering.cc",
                 google_client_ids=["my-client-id.apps.googleusercontent.com"])
    assert s.social_signin_enabled is True


def test_dev_passkey_rp_id_mismatch_boots():
    # Dev is exempt (guard is prod-only) — a mismatched rp_id in dev still boots.
    s = Settings(_env_file=None, environment="dev", passkey_enabled=True,
                 gateway_base_url="https://chat.enspyr.co",
                 passkey_rp_id="chat.imagineering.cc")
    assert s.is_production is False


# --- island identity + mode (crucible-09 Phase A: A1 + A2) ------------------- #

def test_e2ee_mode_rejected_in_production():
    # A2: e2ee is schema-reserved for Phase B — no encryption exists, so advertising
    # it would mislabel operator-readable plaintext as E2EE. Refuse boot.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 island_mode="e2ee")


def test_e2ee_mode_rejected_in_dev_too():
    # A2 is NOT prod-gated: a dev/test island advertising e2ee would still lie to its
    # client (A3 reads the manifest in dev). The mislabel is environment-independent.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", island_mode="e2ee")


def test_moderator_mode_is_the_default_and_boots():
    s = Settings(_env_file=None, environment="dev")
    assert s.island_mode == "moderator"


def test_prod_with_dev_island_seed_raises():
    # The island key is a trust root (signs the self-manifest); the dev default in
    # prod would let anyone who read the repo sign a manifest for this island.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_DEV_ISLAND_SEED)


def test_prod_with_real_island_seed_boots():
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED)
    assert s.is_production is True


def test_malformed_island_seed_rejected_all_env():
    # A key that can't decode to 32 bytes can't sign — fail closed at boot in EVERY
    # environment, not 500 on the first /v1/island fetch.
    short = base64.urlsafe_b64encode(b"too-short").rstrip(b"=").decode()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", island_signing_seed=short)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", island_signing_seed="has spaces!")


def test_dev_accepts_the_dev_island_seed():
    # Local dev must stay frictionless: the dev-default seed boots in dev (only prod
    # rejects it). Passed explicitly because conftest seeds a real ISLAND_SIGNING_SEED
    # into the env, so a bare Settings() would read that, not the dev default.
    s = Settings(_env_file=None, environment="dev", island_signing_seed=_DEV_ISLAND_SEED)
    assert s.island_mode == "moderator"
    assert s.island_signing_seed == _DEV_ISLAND_SEED
    assert s.is_production is False
