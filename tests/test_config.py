"""Fail-closed configuration guards (prod auth hardening).

Invariants (all prod-only unless noted):
  1. A production-like deployment MUST NOT boot with the dev-default/weak
     jwt_secret — `Settings(_env_file=None)` raises rather than serving forgeable tokens.
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
  7. Every operator-settable env var (secrets, island identity/trust-root, operator
     policy) is FORWARDED into the chat-island container by docker-compose with host
     passthrough (`${VAR...}`) — a value in the host .env is otherwise inert. A
     behavioral tripwire against the recurring "compose dropped the var" class
     (PASSKEY_EXTRA_ORIGINS, MODERATOR_USER_IDS, ISLAND_SIGNING_SEED). Curated
     surface, not a model_fields derivation (#26 orbit). Cage-match PR#110.

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

# A5 (crucible-09): production moderator mode (the DEFAULT mode) refuses to boot without a
# configured moderator AND the CSAM-runbook ack. Every "valid prod baseline" construction
# spreads this so the A5 capstone gate is satisfied and the test isolates the invariant it
# actually varies. A future prod requirement is added HERE once, not copy-pasted across
# every boots test. (Negative/`raises` prod tests deliberately OMIT it — the A5 gate is the
# last prod check, so those tests still surface their own earlier invariant's failure.)
_PROD_MOD = dict(moderator_user_ids=["mod-user-01"], csam_runbook_acknowledged=True)


def test_prod_with_real_secret_boots():
    # passkey_enabled makes this a joinable island — invariant 5 (below) now
    # requires a viable ingress in prod, so a password-only prod is a locked
    # island. This test is about the jwt_secret invariant, so give it an ingress.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, **_PROD_MOD)
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


# --- LiveKit prod fail-closed (cage-match #122 rd2) --------------------------
# WHEN configured, LiveKit mints bearer capabilities to a SHARED SFU, so prod boots
# demand a strong secret + a island_id namespace + wss + both-or-neither creds.

_LK_SECRET_STRONG = "livekit-prod-secret-32-bytes-plus!"  # 34 chars >= 32


def _prod_lk(**over):
    base = dict(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                passkey_enabled=True, livekit_api_key="APIabc123",
                livekit_api_secret=_LK_SECRET_STRONG, island_id="island-a", **_PROD_MOD)
    base.update(over)
    return Settings(**base)


def test_prod_livekit_fully_configured_boots():
    s = _prod_lk()
    assert s.livekit_api_key == "APIabc123" and s.island_id == "island-a"


def test_prod_livekit_without_gateway_id_raises():
    # Empty island_id on a shared SFU = fail-open cross-island room/identity collision.
    with pytest.raises(ValidationError):
        _prod_lk(island_id="")


def test_prod_livekit_weak_secret_raises():
    with pytest.raises(ValidationError):
        _prod_lk(livekit_api_secret="short")


def test_prod_livekit_half_configured_raises():
    # Key set, secret empty (or vice versa) → refuse rather than silently 503-at-runtime.
    with pytest.raises(ValidationError):
        _prod_lk(livekit_api_secret="")


def test_prod_livekit_non_wss_url_raises():
    with pytest.raises(ValidationError):
        _prod_lk(livekit_url="ws://livekit.example.cc")


def test_prod_without_livekit_boots_unaffected():
    # The guard is skipped entirely when LiveKit is unconfigured — no island_id needed.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, **_PROD_MOD)
    assert s.livekit_api_key == "" and s.is_production is True


def test_nonprod_livekit_remote_sfu_without_gateway_id_raises():
    # Wu F1: the shared-SFU guard is NOT gated on ENVIRONMENT. A test/dev box with the
    # real shared creds pointed at the remote SFU + no island_id would merge its users
    # into prod's rooms — so it must fail closed in a NON-prod env too.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", jwt_secret=_STRONG_SECRET,
                 livekit_api_key="APIabc", livekit_api_secret=_LK_SECRET_STRONG,
                 livekit_url="wss://livekit.imagineering.cc", island_id="")


def test_livekit_loopback_sfu_is_exempt():
    # A loopback dev SFU carries no cross-island collision / forgery risk, so the
    # island_id / wss / secret-strength guards relax (ws://localhost, short secret ok).
    s = Settings(_env_file=None, environment="dev", jwt_secret=_DEV_JWT_SECRET,
                 livekit_api_key="devkey", livekit_api_secret="short-dev",
                 livekit_url="ws://localhost:7880", island_id="")
    assert s.livekit_api_key == "devkey" and s.island_id == ""


def test_livekit_url_empty_host_raises():
    # Wu F3: "wss://" passes a startswith check but has no host — reject it.
    with pytest.raises(ValidationError):
        _prod_lk(livekit_url="wss://")


def test_prod_loopback_sfu_is_not_exempt():
    # Carnot rd6: the loopback exemption must NOT reach production. A prod island on
    # ws://localhost is misconfigured (mobile clients resolve localhost to the device)
    # or a hardening bypass — so prod gets the full guards regardless of host.
    with pytest.raises(ValidationError):
        _prod_lk(livekit_url="ws://localhost:7880")


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
                 passkey_enabled=True, **_PROD_MOD)
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
                 google_client_ids=["my-client-id.apps.googleusercontent.com"], **_PROD_MOD)
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
                 github_client_id="gh-id", github_client_secret="gh-secret", **_PROD_MOD)
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
                 github_client_id="gh-id", github_client_secret="gh-secret", **_PROD_MOD)
    assert s.github_client_id == "gh-id"


def test_prod_broker_neither_set_boots():
    # No broker provider configured at all is fine — the provider is simply absent.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, **_PROD_MOD)
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
                 passkey_enabled=True, social_signin_enabled=False, **_PROD_MOD)
    assert s.passkey_enabled is True


def test_prod_social_only_boots():
    # Social alone (with a provider) is a complete ingress — passkey may stay dark.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=False, social_signin_enabled=True,
                 google_client_ids=["my-client-id.apps.googleusercontent.com"], **_PROD_MOD)
    assert s.social_signin_enabled is True


def test_prod_both_ingresses_boot():
    # The passkey-migration steady state: both on. The #1923 retirement flips
    # social off only AFTER passkey is enabled, so this guard is never tripped in a
    # correct migration — only by turning BOTH off (the bug).
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, social_signin_enabled=True,
                 google_client_ids=["my-client-id.apps.googleusercontent.com"], **_PROD_MOD)
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
                 passkey_rp_id="chat.enspyr.co", **_PROD_MOD)
    assert s.passkey_rp_id == "chat.enspyr.co"


def test_prod_passkey_rp_id_registrable_parent_boots():
    # WebAuthn allows rp_id to be a registrable PARENT of the serving host — a
    # credential scoped to example.com is usable on chat.example.com. Must boot.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, gateway_base_url="https://chat.example.com",
                 passkey_rp_id="example.com", **_PROD_MOD)
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
                 passkey_enabled=True, **_PROD_MOD)  # base_url + rp_id both default to chat.imagineering.cc
    assert s.passkey_enabled is True


def test_prod_passkey_disabled_skips_rp_id_check():
    # Passkey off → the rp_id check is irrelevant; social carries the ingress.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=False, social_signin_enabled=True,
                 gateway_base_url="https://chat.enspyr.co",
                 passkey_rp_id="chat.imagineering.cc",
                 google_client_ids=["my-client-id.apps.googleusercontent.com"], **_PROD_MOD)
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


# --- A5 (crucible-09): "moderator = commitment" election gate ---------------- #
#
# In PRODUCTION, electing moderator mode (the default) requires BOTH a configured
# moderator AND the CSAM-runbook ack, so the "plaintext with moderation deleted" config
# is unbootable. Prod-only: dev runs moderator freely. Each `raises` test isolates ONE
# cause by satisfying the other (the A5 gate is the LAST prod check, so these also clear
# every earlier invariant first).

def test_prod_moderator_mode_without_moderator_raises():
    # The disable-vector: a prod moderator island with an EMPTY moderator set has a
    # write-only report queue nobody can act on (require_moderator 403s everyone). Ack IS
    # set, so this isolates the moderator-required cause. `match=` pins the diagnostic
    # contract so a future gate inserted after A5 can't let this green on the wrong error
    # (Tesla, PR#113 cage-match).
    with pytest.raises(ValidationError, match="MODERATOR_USER_IDS"):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 csam_runbook_acknowledged=True, moderator_user_ids=[])


def test_prod_moderator_mode_with_whitespace_only_moderator_raises():
    # A whitespace-only id is not a real moderator — boundary normalization compacts it to
    # an empty set, which A5 then refuses (same path as an empty MODERATOR_USER_IDS).
    with pytest.raises(ValidationError, match="MODERATOR_USER_IDS"):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 csam_runbook_acknowledged=True, moderator_user_ids=["   "])


def test_moderator_user_ids_normalized_at_boundary():
    # Asymmetric-strip fix (Tesla, PR#113): the STORED list is stripped + compacted at the
    # Settings boundary, so it equals what runtime is_moderator matches against. A padded
    # id keeps its identity (stripped), a whitespace-only entry is dropped.
    s = Settings(_env_file=None, environment="dev",
                 moderator_user_ids=["  mod-01  ", "   ", "mod-02"])
    assert s.moderator_user_ids == ["mod-01", "mod-02"]


def test_prod_moderator_padded_id_boots_with_stripped_value():
    # A padded id must NOT be the empty-set vector in costume: it normalizes to a clean id
    # that runtime is_moderator can actually match, and boots (moderator present + ack).
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 moderator_user_ids=["  real-ulid  "], csam_runbook_acknowledged=True)
    assert s.moderator_user_ids == ["real-ulid"]  # stored value is the clean, matchable id


def test_prod_moderator_ghost_seat_boots_known_residual():
    # PINS THE SCOPE (Tesla + Carnot, PR#113): A5 closes the EMPTY-set + UNACKNOWLEDGED
    # vectors, NOT the ghost-seat one. A boot-time Settings validator has no DB, so a
    # well-formed-but-never-registered moderator id passes the presence gate and BOOTS —
    # yet require_moderator would 403 it at runtime, leaving moderation effectively off.
    # This is asserted (not just commented) so the honest scope is test-enforced and a
    # future change to it is conscious; seat LIVENESS is a runtime invariant (task: runtime
    # moderator seat-health), NOT something this boot gate can or claims to guarantee.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 moderator_user_ids=["never-registered-ulid"], csam_runbook_acknowledged=True)
    assert s.island_mode == "moderator"  # boots: A5 does NOT (and cannot) verify liveness


def test_prod_moderator_mode_without_runbook_ack_raises():
    # A moderator is configured but the CSAM runbook is unacknowledged — electing
    # moderator mode is a commitment; refuse boot until acknowledged. Isolates the
    # ack-required cause (moderator IS set).
    with pytest.raises(ValidationError, match="CSAM_RUNBOOK_ACKNOWLEDGED"):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 moderator_user_ids=["mod-user-01"], csam_runbook_acknowledged=False)


def test_prod_moderator_mode_with_moderator_and_ack_boots():
    # BOTH satisfied → the valid prod moderator election boots.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED,
                 moderator_user_ids=["mod-user-01"], csam_runbook_acknowledged=True)
    assert s.csam_runbook_acknowledged is True and s.island_mode == "moderator"


def test_dev_moderator_mode_exempt_from_commitment_gate():
    # Prod-only: a dev island runs the default moderator mode with NO moderator and NO
    # ack — the commitment gate must not fire (moderator is the default; an every-env
    # gate would break every dev boot).
    s = Settings(_env_file=None, environment="dev")
    assert s.island_mode == "moderator"
    assert s.moderator_user_ids == [] and s.csam_runbook_acknowledged is False


def test_csam_runbook_ack_defaults_false():
    # Fail-closed default: acknowledgement must be conscious, never an inherited truthy.
    s = Settings(_env_file=None, environment="dev")
    assert s.csam_runbook_acknowledged is False


def test_prod_with_dev_island_seed_raises():
    # The island key is a trust root (signs the self-manifest); the dev default in
    # prod would let anyone who read the repo sign a manifest for this island.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_DEV_ISLAND_SEED)


def test_prod_rejects_noncanonical_dev_seed_alias():
    # Carnot HIGH: a non-canonical base64url alias of the dev seed decodes to the SAME
    # (public) dev KEY but is a different string, so it would slip past the
    # string-equality dev-seed guard and boot prod on the known key. The canonical
    # decoder now rejects the alias at the seed-decode step, before the guard.
    alias = _DEV_ISLAND_SEED[:-1] + ("F" if _DEV_ISLAND_SEED[-1] != "F" else "G")
    import base64
    assert alias != _DEV_ISLAND_SEED
    assert base64.urlsafe_b64decode(alias + "=") == base64.urlsafe_b64decode(_DEV_ISLAND_SEED + "=")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=alias)


def test_prod_with_real_island_seed_boots():
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, island_signing_seed=_REAL_ISLAND_SEED, **_PROD_MOD)
    assert s.is_production is True


def test_malformed_island_seed_rejected_all_env():
    # A key that can't decode to 32 bytes can't sign — fail closed at boot in EVERY
    # environment, not 500 on the first /v1/island fetch.
    short = base64.urlsafe_b64encode(b"too-short").rstrip(b"=").decode()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", island_signing_seed=short)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", island_signing_seed="has spaces!")


def test_island_key_version_out_of_u32_range_rejected_at_boot():
    # The manifest packs key_version as a big-endian u32; an out-of-range value must
    # fail closed at boot, not raise struct.error as a 500 on the first /v1/island.
    for bad in (0, -1, 2**32):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, environment="dev", island_key_version=bad)


def test_dev_accepts_the_dev_island_seed():
    # Local dev must stay frictionless: the dev-default seed boots in dev (only prod
    # rejects it). Passed explicitly because conftest seeds a real ISLAND_SIGNING_SEED
    # into the env, so a bare Settings(_env_file=None) would read that, not the dev default.
    s = Settings(_env_file=None, environment="dev", island_signing_seed=_DEV_ISLAND_SEED)
    assert s.island_mode == "moderator"
    assert s.island_signing_seed == _DEV_ISLAND_SEED
    assert s.is_production is False


# --- invariant 7: operator-settable vars MUST be forwarded in docker-compose --
#
# The prod image reads config from the container env. docker-compose injects a
# variable into the container ONLY if the chat-island service references it AND the
# reference passes the host value through (`${VAR...}`); a value sitting in the host
# .env is otherwise inert. This class has bitten repeatedly — PASSKEY_EXTRA_ORIGINS
# (Android passkey CREATE silently failed until forwarded, verified live 2026-07-28),
# MODERATOR_USER_IDS (operator seat #100 empty as deployed), ISLAND_SIGNING_SEED (a
# signed-manifest image would crash-loop the crucible-09 deploy on the dev-seed guard
# because the operator's seed never arrived). A per-var comment is not enforcement
# (cf. the single-worker guard #46). This is the behavioral tripwire.
#
# STRENGTH (cage-match PR#110, Carnot HIGH + Tesla): the check parses the YAML and
# inspects the chat-island `environment:` MAPPING, and requires each var's value to
# be the interpolation form `${VAR...}` — so a bare `VAR:` in a comment, in another
# service, or a static `VAR: literal` (which does NOT pass a host .env value through)
# can no longer green CI. It measures forwarding, not ink.
#
# SCOPE (honest, per Carnot MEDIUM + Tesla): this is a CURATED operator-config
# surface, NOT a proof that config.py has no un-forwarded operator field. A genuinely
# complete derivation needs per-field "operator-settable" metadata on Settings
# (follow-up #26 orbit). TTLs, host/port, and code-defaulted infra knobs are
# deliberately excluded; DB_URL / AIKO_MQTT_* / ENVIRONMENT are static-by-design
# (not host-overridable) so they are excluded too.
# _MUST_FORWARD_ENV DELETED (cage-match PR#141 round 5, Tesla). It listed vars that
# "may never be excluded" — and was itself a curated allowlist that someone had to
# remember to extend, which is the exact class this file exists to kill. Round 3 added
# AUTH_RATE_LIMIT but not its window; round 4 patched that and still left the passkey
# TTLs, the JWT TTLs and SOCIAL_NONCE_REQUIRED ordinary. A partial never-exclude list
# gives false assurance about the fields it forgot.
#
# It is also fully SUBSUMED: invariant 7b already asserts that every Settings field is
# forwarded with a host-passthrough interpolation UNLESS it is named in _NOT_FORWARDED
# with a substantive reason. "Forwarded XOR justified exclusion" is the one honest
# partition, and it is total. Keeping a second, hand-maintained list beside it added
# no coverage and one more thing to forget.
#
# The incidents that motivated the original list are kept in the invariant-7 comment
# above: PASSKEY_EXTRA_ORIGINS, MODERATOR_USER_IDS, ISLAND_SIGNING_SEED, APNS_*.


def _chat_island_environment() -> dict[str, str]:
    """The parsed chat-island `environment:` mapping from docker-compose.yml."""
    import yaml
    from pathlib import Path

    compose = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    )
    env = compose["services"]["chat-island"]["environment"]
    # Compose allows a list ("- VAR=val") or a map; this template uses a map. Assert
    # it, so a future switch to list form fails LOUDLY here rather than silently
    # neutering every check below (which assumes dict membership).
    assert isinstance(env, dict), (
        "chat-island `environment:` is not a mapping — the forwarding checks below "
        "assume map form; update them if the compose switches to list syntax."
    )
    return {k: str(v) for k, v in env.items()}


def test_compose_island_signing_seed_default_matches_config_dev_seed():
    # The signing-seed forward bakes config._DEV_ISLAND_SEED as the compose default
    # so the forward is a zero-regression superset (unset → the friendly prod
    # dev-seed guard, not a cryptic decode error). That default is a SECOND copy of
    # the sentinel; if config.py rotates _DEV_ISLAND_SEED and compose keeps injecting
    # the OLD base64url, a prod-unset boot would run on a repo-public key that the
    # app-level guard no longer denylists (cage-match PR#110, Tesla). Enforce the
    # sync with a test instead of a "keep in sync" comment.
    import re

    env = _chat_island_environment()
    m = re.fullmatch(r"\$\{ISLAND_SIGNING_SEED:-(.*)\}", env["ISLAND_SIGNING_SEED"])
    assert m, (
        "ISLAND_SIGNING_SEED is not the expected `${ISLAND_SIGNING_SEED:-<default>}` "
        f"form: {env['ISLAND_SIGNING_SEED']!r}"
    )
    assert m.group(1) == _DEV_ISLAND_SEED, (
        "docker-compose.yml's ISLAND_SIGNING_SEED default has drifted from "
        "config._DEV_ISLAND_SEED. A prod-unset boot would then miss the friendly "
        "dev-seed guard (or, worse, run on published key material no longer "
        "denylisted). Update the compose default to match config._DEV_ISLAND_SEED."
    )


# --- invariant 7b: the forwarding check is TOTAL, not curated ------------------
#
# _MUST_FORWARD_ENV above is a hand-maintained allowlist, and its own scope note
# admitted the gap: "a CURATED operator-config surface, NOT a proof that config.py
# has no un-forwarded operator field." On 2026-08-22 that gap was measured — 30 of
# 62 Settings fields were not forwarded at all, including OPEN_REGISTRATION,
# RATE_LIMIT_ENABLED, AUTH_RATE_LIMIT, MAX_REQUEST_BYTES and
# PASSKEY_REQUIRE_USER_VERIFICATION. An allowlist can only ever assert the vars
# somebody remembered to add, so it cannot fail for the vars nobody thought of —
# which is precisely the failure mode (four incidents and counting).
#
# So invert the burden of proof: EVERY Settings field must be forwarded, and any
# exception has to be written down here with a reason. Adding a new setting now
# fails CI until its author makes that choice explicitly.
#
# The concern that motivated a curated "never exclude these" list is real — this test
# IS satisfiable by widening _NOT_FORWARDED — but the answer is not a second
# hand-maintained list (see the _MUST_FORWARD_ENV deletion note below). It is
# test_the_exclusion_set_is_closed: the exclusions are FROZEN, so widening them takes
# two deliberate edits and shows up twice in a diff.

_NOT_FORWARDED: dict[str, str] = {
    # Container-internal bind address/port. The image listens inside the container
    # and compose owns the published mapping; an operator overriding these from the
    # host .env would move the listener out from under the port mapping and the
    # healthcheck, breaking the box in a way no error message explains.
    "HOST": "container-internal bind — compose owns the port mapping",
    "PORT": "container-internal bind — compose owns the port mapping",
    # Deliberately NOT operator-tunable. Making the JWT algorithm settable from the
    # environment is the classic algorithm-confusion footgun; there is no legitimate
    # per-island reason to change it, and every illegitimate one is an attack.
    "JWT_ALGORITHM": "security-critical constant — alg-confusion footgun if tunable",
    # Static-by-design: these describe the compose project's OWN topology, not
    # operator policy. The mosquitto hostname is the service name on this network;
    # DB_URL points at the mounted volume; ENVIRONMENT is what makes the image a
    # production image. A host .env must not be able to repoint them.
    "AIKO_MQTT_HOST": "compose-topology-owned — the service name on this network",
    "AIKO_MQTT_PORT": "compose-topology-owned",
    "AIKO_NAMESPACE": "compose-topology-owned",
    "DB_URL": "compose-topology-owned — the mounted aiko_data volume",
    "ENVIRONMENT": "static-by-design — this is what makes the image a prod image",
}

# Fields where the compose default INTENTIONALLY differs from config.py's default.
# Everywhere else they must agree, because a compose default is not documentation:
# `${VAR:-}` does not "fall back" to the config default, it SETS the container var to
# the empty string and overrides it — usually into a boot-time pydantic error.
_DEFAULT_MAY_DIVERGE: dict[str, tuple[str, str]] = {
    # var -> (EXACT compose default expected, why it differs from config.py)
    #
    # The value is pinned, not just the reason (cage-match PR#141 round 5, Tesla).
    # Exempting a field from the parity check exempted it from EVERY check on that
    # value: `OPEN_REGISTRATION: ${OPEN_REGISTRATION:-false}` could be edited to
    # `true` and CI would stay green while both production islands opened
    # registration to anyone. Same for the baked Android fingerprints and OAuth
    # client IDs — still "diverging", still wrong, still silent. An exemption that
    # does not state the expected spelling is a fuse already pulled.
    "OPEN_REGISTRATION": (
        "false",
        "compose sends the RESOLVED prod value, not the tri-state None. A blank "
        "default crash-loops any older pinned image lacking the coercion — verified "
        "against 0.6.0. See the comment in docker-compose.yml.",
    ),
    "JWT_SECRET": (
        None,
        "uses the REQUIRED `${VAR:?...}` form — no default by design",
    ),
    "APPLE_CLIENT_IDS": (
        '["cc.imagineering.aikoChatApp"]',
        "compose ships the real app identifiers; config default is []",
    ),
    "GOOGLE_CLIENT_IDS": (
        '["588100172166-a0r6ipvpju5rd6fh8lv85qqohpdosp33.apps.googleusercontent.com","588100172166-23ppgk6jdb3et2ehc1qj584c6rgs0c9j.apps.googleusercontent.com","588100172166-bagrmvftp3fcer2j2lvnh33j65g58elk.apps.googleusercontent.com"]',
        "compose ships the real OAuth client IDs; config default is []. Pinned "
        "despite the length: an unpinned exemption means nothing checks the baked "
        "value at all, and a transposed character in an audience is invisible.",
    ),
    "PASSKEY_EXTRA_ORIGINS": (
        '["android:apk-key-hash:4X-Pyy5caiOFWCJxYkU2YhIdsfnYZqG57ETE7JFgi6A","android:apk-key-hash:jDaX5vI4HYYMxrQkOUtGeSK9hcTgeuP52fGEQaptSOc","android:apk-key-hash:r_tsz5JUp0Rq8qioELOr73CZt0IegpLHz3sw6aI_p3A"]',
        "compose ships the Android apk-key-hash origins; config default is []",
    ),
    "PASSKEY_ANDROID_CERT_SHA256": (
        '["E1:7F:8F:CB:2E:5C:6A:23:85:58:22:71:62:45:36:62:12:1D:B1:F9:D8:66:A1:B9:EC:44:C4:EC:91:60:8B:A0","8C:36:97:E6:F2:38:1D:86:0C:C6:B4:24:39:4B:46:79:22:BD:85:C4:E0:7A:E3:F9:D9:F1:84:41:AA:6D:48:E7","AF:FB:6C:CF:92:54:A7:44:6A:F2:A8:A8:10:B3:AB:EF:70:99:B7:42:1E:82:92:C7:CF:7B:30:E9:A2:3F:A7:70"]',
        "compose ships the Play signing cert fingerprints; config default is []",
    ),
}


class _RequiredField(Exception):
    """Raised by _render_default for a field with no default at all."""


def _render_default(default) -> str:
    """Render a pydantic default as the string a compose default must equal.

    Raises _RequiredField for a REQUIRED field. Without that, `str(PydanticUndefined)`
    renders the literal "PydanticUndefined" and the parity test below demands compose
    read `${VAR:-PydanticUndefined}` — instructing the author to write nonsense instead
    of telling them the truth, which is that a required field takes the `${VAR:?...}`
    form and carries no default. JWT_SECRET is the only one today and is exempted, so
    this is latent until the next required setting is added.
    """
    import json

    from pydantic_core import PydanticUndefined

    if default is PydanticUndefined:
        raise _RequiredField
    if default is None:
        return ""
    if isinstance(default, bool):
        return str(default).lower()
    if isinstance(default, (list, dict)):
        return json.dumps(default)
    return str(default)


def _accepts_empty_string_without_coercion(annotation) -> bool:
    """True only for types that parse "" on an image WITHOUT config's blank coercion.

    A SAFE-list on purpose (cage-match PR#141 round 7, Tesla): anything unrecognised
    returns False and is therefore reported as unsafe. The failure direction matters —
    a false positive here costs one explicit compose default; a false negative is a
    boot-time crash-loop on a pinned island, which is the outage this PR started from.
    """
    import typing

    args = typing.get_args(annotation)
    if args:
        # A union is safe only if EVERY non-None arm can take "".
        return all(
            a is type(None) or _accepts_empty_string_without_coercion(a) for a in args
        )
    # Plain `str` (and str subclasses that are not enums) accept "" verbatim. Nothing
    # else is assumed to: an enum rejects it, and so do int/float/bool/UUID/Path/
    # HttpUrl/datetime and every Literal.
    import enum

    return (
        isinstance(annotation, type)
        and issubclass(annotation, str)
        and not issubclass(annotation, enum.Enum)
    )


def _is_complex_field(annotation) -> bool:
    """True for a field pydantic-settings JSON-decodes in the ENV SOURCE.

    Decided by type introspection, not by looking for the letters "list"/"dict" in
    `str(annotation)` (cage-match PR#141 round 4, Tesla). Production stopped
    classifying by substring in round 3; the TEST kept doing it, which is the same lab
    coat on the instrument rather than the subject — and the instrument is the half
    that can bless a crash. A `Sequence[str]` would not have matched "list" and would
    have been reported as a coercion failure no validator could ever fix.
    """
    import collections.abc as _abc
    import typing

    origin = typing.get_origin(annotation)
    if origin is None:
        return isinstance(annotation, type) and issubclass(
            annotation, (list, dict, set, tuple)
        ) and not issubclass(annotation, str)
    if origin in (list, dict, set, tuple, frozenset):
        return True
    if isinstance(origin, type) and issubclass(
        origin, (_abc.Sequence, _abc.Mapping, _abc.Set)
    ) and not issubclass(origin, str):
        return True
    # Unions: complex if ANY arm is complex (the env source decodes on the arm).
    return any(_is_complex_field(a) for a in typing.get_args(annotation))


def _render_field_default(field) -> str:
    """Render a FIELD's effective default, honouring default_factory.

    cage-match PR#141 round 3 (Tesla): `Field(default_factory=list)` also reports
    `default is PydanticUndefined`, so treating that sentinel as "required" would tell
    the author of the next modern-style list setting to convert it to `${VAR:?}` — a
    compose-time hard failure for a field that has a perfectly good default. Consult
    the factory before concluding a field is required.
    """
    from pydantic_core import PydanticUndefined

    if getattr(field, "default_factory", None) is not None:
        return _render_default(field.default_factory())
    if field.default is PydanticUndefined:
        raise _RequiredField
    return _render_default(field.default)


def _settings_field_names() -> set[str]:
    from aiko_gateway.config import Settings

    return {name.upper() for name in Settings.model_fields}


def test_every_settings_field_is_forwarded_or_explicitly_excluded():
    """No Settings field may be silently absent from the compose environment."""
    import re

    env = _chat_island_environment()
    unaccounted = []
    for var in sorted(_settings_field_names()):
        if var in _NOT_FORWARDED:
            continue
        if var not in env:
            unaccounted.append(f"{var} (absent from compose)")
        elif not re.search(r"\$\{" + re.escape(var) + r"[-:}]", env[var]):
            unaccounted.append(f"{var} (present but static — host value inert)")
    assert not unaccounted, (
        "These Settings fields are not forwarded into the chat-island container, so "
        "an operator setting them in the host .env would have NO EFFECT and no error "
        f"to explain why: {unaccounted}. Either add a `VAR: ${{VAR:-<config default>}}` "
        "forward, or add the name to _NOT_FORWARDED with a reason. See invariant 7b."
    )


def test_no_stale_entries_in_not_forwarded():
    """_NOT_FORWARDED must not name fields that no longer exist."""
    stale = sorted(set(_NOT_FORWARDED) - _settings_field_names())
    assert not stale, (
        f"_NOT_FORWARDED names non-existent Settings field(s): {stale}. A stale "
        "exclusion silently widens the exemption if the name is ever reused."
    )


def test_compose_defaults_match_config_defaults():
    """A compose default overrides the config default — so it must equal it."""
    import re

    from aiko_gateway.config import Settings

    env = _chat_island_environment()
    drifted = []
    for name, field in Settings.model_fields.items():
        var = name.upper()
        if var in _NOT_FORWARDED or var not in env:
            continue
        if var in _DEFAULT_MAY_DIVERGE:
            # Exempt from matching CONFIG's default — but not from having its own
            # value checked. Where a spelling is pinned, it is enforced here.
            pinned = _DEFAULT_MAY_DIVERGE[var][0]
            if pinned is not None:
                m = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", env[var], re.S)
                got = m.group(1) if m else env[var]
                if got != pinned:
                    drifted.append(
                        f"{var}: compose default is {got!r} but _DEFAULT_MAY_DIVERGE "
                        f"pins it to {pinned!r}. Change the pin deliberately, in "
                        "review — this value is exempt from config parity, so this "
                        "assertion is the ONLY thing checking it."
                    )
            continue
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", env[var], re.S)
        if match is None:
            continue  # a non-defaulted form (e.g. `${VAR}`); nothing to compare
        try:
            expected = _render_field_default(field)
        except _RequiredField:
            # A required field forwarded WITH a default is its own bug: the compose
            # default satisfies the field on every box that does not set it, so the
            # "required" is silently discharged by the template and the operator is
            # never told. Say that, rather than demanding a rendered default.
            drifted.append(
                f"{var}: config has NO default (required), but compose supplies "
                f"{match.group(1)!r} — a required setting must use the "
                f"`${{{var}:?why it is required}}` form so a missing value FAILS "
                "loudly, not silently defaults"
            )
            continue
        if match.group(1) != expected:
            drifted.append(
                f"{var}: compose={match.group(1)!r} config={expected!r}"
            )
    assert not drifted, (
        "compose defaults have drifted from config.py defaults: "
        f"{drifted}. The compose default is the value the container actually gets "
        "when the operator sets nothing — it does not defer to config.py. Fix the "
        "compose default, or record the divergence in _DEFAULT_MAY_DIVERGE with a "
        "reason. See invariant 7b."
    )


def test_open_registration_blank_behaves_as_unset():
    """The tri-state field must survive compose's inability to express absence.

    docker-compose's `environment:` mapping has no "omit if unset": an unset host var
    interpolates to the empty string and the container var is still SET. Since
    invariant 7b requires OPEN_REGISTRATION to be forwarded, "" reaches pydantic on
    every deploy where the operator did not set it. Before config.py coerced it, that
    was a ValidationError — i.e. a crash-loop on both live islands.
    """
    import os
    from unittest import mock

    from aiko_gateway.config import Settings

    # Through the ENVIRONMENT, not constructor kwargs (Carnot round 6). The kwarg path
    # skips the env source entirely, so source/validator ORDERING changes — the exact
    # mechanism that puts complex fields beyond reach — would not fail this test even
    # though they would break the real thing. Test the channel the value arrives on.
    def via_env(value):
        with mock.patch.dict(os.environ, {"OPEN_REGISTRATION": value}, clear=False):
            return Settings(_env_file=None).open_registration

    def unset():
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPEN_REGISTRATION", None)
            return Settings(_env_file=None).open_registration

    assert via_env("") == unset(), (
        "blank OPEN_REGISTRATION must behave exactly as if it were unset"
    )
    # and an explicit value must still win in both directions
    assert via_env("false") is False
    assert via_env("true") is True


def test_no_stale_entries_in_default_may_diverge():
    """A divergence exemption must still name a field that still diverges.

    Asymmetry caught by the invariant-lattice pass: _NOT_FORWARDED had a staleness
    check and _DEFAULT_MAY_DIVERGE did not. Both are exemption lists, and a stale
    exemption is worse than a missing one — it silently disarms the drift check for a
    name that may later be reused, or keeps excusing a divergence someone has since
    fixed, so the next real drift in that field goes unreported forever.

    It found one on arrival: ISLAND_SIGNING_SEED was listed here while its compose
    default in fact MATCHES config._DEV_ISLAND_SEED (which is precisely what
    test_compose_island_signing_seed_default_matches_config_dev_seed asserts). The
    exemption was excusing a divergence that did not exist, and disarming the check.
    """
    import json
    import re

    from aiko_gateway.config import Settings

    stale = sorted(set(_DEFAULT_MAY_DIVERGE) - _settings_field_names())
    assert not stale, (
        f"_DEFAULT_MAY_DIVERGE names non-existent Settings field(s): {stale}."
    )

    env = _chat_island_environment()
    no_longer_diverging = []
    for var in sorted(_DEFAULT_MAY_DIVERGE):
        name = var.lower()
        if var not in env or name not in Settings.model_fields:
            continue
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", env[var], re.S)
        try:
            effective = _render_field_default(Settings.model_fields[name])
        except _RequiredField:
            continue  # required fields legitimately have no default to compare
        if match and match.group(1) == effective:
            no_longer_diverging.append(var)
    assert not no_longer_diverging, (
        "These are listed in _DEFAULT_MAY_DIVERGE but their compose default now "
        f"MATCHES config.py: {no_longer_diverging}. Remove the exemption so the field "
        "is protected by the drift check again — a stale exemption is a permanently "
        "disarmed assertion."
    )


def test_every_forward_states_a_default_or_is_explicitly_required():
    """A bare `${VAR}` forward is a hole in the induction step.

    cage-match PR#141 round 2 (Carnot, HIGH). Invariant 7b accepts `${VAR}` as a
    valid forward — the boundary character class allows the closing brace — but
    compose does NOT omit the variable when the host has not set it: it resolves to
    the empty string and STILL overrides the pydantic default. Meanwhile both
    downstream checks (`..._defaults_match_config_defaults` and
    `..._every_compose_default_is_constructible`) `continue` past any value that is
    not `${VAR:-...}`, because there is no default to compare. So a future
    `RATE_LIMIT_ENABLED: ${RATE_LIMIT_ENABLED}` would pass every assertion in this
    file and reopen the exact class under a different interpolation form.

    No such forward exists today — this closes it while it is still latent. Two forms
    are legitimate and must be stated explicitly: `${VAR:-<config default>}` (has a
    default) or `${VAR:?reason}` (required, fails loudly when missing).
    """
    import re

    from aiko_gateway.config import Settings

    env = _chat_island_environment()
    bare = []
    for name in Settings.model_fields:
        var = name.upper()
        if var not in env or var in _NOT_FORWARDED:
            continue
        value = env[var]
        if not re.search(r"\$\{" + re.escape(var) + r"[-:}]", value):
            continue  # static literal — a different check's business
        # ALLOWLIST the two lawful forms rather than denylisting the one bad spelling
        # (cage-match PR#141 round 4, Tesla). compose has a third expansion,
        # `${VAR-default}` — hyphen, no colon — which substitutes only when the var is
        # UNSET, not when it is empty. So a `.env` line reading `FOO=` lands in the
        # container as "" and dies on the next int/bool, while every check in this
        # file stays green: 7b's passthrough class invites `-`, and both the parity and
        # constructibility tests `fullmatch` on `:-` and skip it. Round 2 closed one
        # spelling of absence; this closes the family.
        if not re.fullmatch(
            r"\$\{" + re.escape(var) + r"(:-.*|:\?.*)\}", value, re.S
        ):
            bare.append(f"{var} = {value!r}")
    assert not bare, (
        f"These forwards do not use one of the two lawful forms: {bare}. Exactly "
        "`${VAR:-<config default>}` (has a default) or `${VAR:?why}` (required) are "
        "allowed. In particular the bare `${VAR}` and the colonless `${VAR-default}` "
        "both let an unset-or-empty host value reach pydantic as the empty string "
        "while skipping the default-parity and constructibility checks."
    )


def test_no_forwarded_var_without_a_settings_field():
    """The mirror direction: a forward for a name config.py does not read.

    A typo'd forward (APNS_TOPICC), or one left behind after a setting is renamed,
    passes every check above — the field it should protect is simply absent, and the
    operator's value lands in the container where nothing reads it. Same inert-value
    outcome as a missing forward, reached from the opposite side, and invisible
    because each half looks fine on its own.
    """
    env = _chat_island_environment()
    orphans = sorted(set(env) - _settings_field_names())
    assert not orphans, (
        f"docker-compose.yml forwards var(s) with no matching Settings field: "
        f"{orphans}. Either config.py lost the field (delete the forward) or the "
        "name is misspelt (the operator's value reaches the container unread)."
    )


def test_every_compose_default_is_constructible():
    """Whatever compose sends when the operator sets NOTHING must parse.

    The general form of the bug cage-match PR#141 (Carnot P1) found in a single
    field. compose's `environment:` mapping cannot express absence — an unset host
    var interpolates to the default and the container var is SET — so every default
    written here is a value pydantic will really receive on a box that configures
    nothing. Invariant 7b *forces* each field to be forwarded; nothing forced the
    forwarded value to be constructible, and the two together can manufacture a
    boot-time crash-loop out of an otherwise correct pair of changes.

    Today all eleven empty-defaulted forwards are `str` / `str | None` / the coerced
    tri-state, so the suite would pass without this. The hazard is the NEXT field: an
    `int` or plain `bool` added with an empty default dies at pydantic on every island
    that has not set it. Quantify the property over the field set rather than pinning
    the one instance we happen to know about.
    """
    import os
    import re
    from unittest import mock

    from aiko_gateway.config import Settings

    env = _chat_island_environment()
    unconstructible = []
    for name in Settings.model_fields:
        var = name.upper()
        if var not in env:
            continue
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", env[var], re.S)
        if match is None:
            continue  # `${VAR}` / `${VAR:?}` — no default to send
        # Deliver the value the way a CONTAINER delivers it: as an environment
        # variable, not a constructor kwarg. The two paths are NOT equivalent —
        # pydantic-settings JSON-decodes complex fields (list[str]) only on the env
        # path, so `Settings(aiko_channels='["general"]')` raises while the identical
        # value in the environment parses fine. An earlier revision of this test used
        # kwargs and reported seven false crash-loops for list fields that work
        # perfectly in production. Test the channel the value actually arrives on.
        with mock.patch.dict(os.environ, {var: match.group(1)}, clear=False):
            try:
                Settings(_env_file=None)
            except Exception as exc:  # noqa: BLE001 — any parse failure is the finding
                unconstructible.append(
                    f"{var}={match.group(1)!r} -> {type(exc).__name__}"
                )
    assert not unconstructible, (
        "These compose defaults cannot be parsed by their own Settings field, so an "
        "island that does not set them CRASH-LOOPS at boot: "
        f"{unconstructible}. Either give the forward a default the field accepts, or "
        "coerce the value at the field boundary (see _blank_is_unspecified)."
    )


def test_exemption_reasons_are_substantive():
    """An exemption is only as good as the reason written beside it.

    Carnot's P2: both exemption dicts are powerful enough to reopen the whole class —
    a future author can silence a totality failure by naming the field with a
    hand-wave. The type system cannot tell a real justification from "", and no test
    can judge prose. What a test CAN do is refuse the degenerate cases, so that
    silencing an invariant costs a sentence someone has to stand behind in review.
    """
    thin = []
    for label, table in (("_NOT_FORWARDED", _NOT_FORWARDED),
                         ("_DEFAULT_MAY_DIVERGE", _DEFAULT_MAY_DIVERGE)):
        for var, entry in table.items():
            reason = entry[1] if isinstance(entry, tuple) else entry
            if len(" ".join(str(reason).split())) < 20:
                thin.append(f"{label}[{var}] = {reason!r}")
    assert not thin, (
        "These exemptions carry no usable justification: "
        f"{thin}. Excluding a setting from the forwarding invariant is how the "
        "inert-config class reopens — write why it is safe, in a sentence."
    )


def test_open_registration_coercion_edges():
    """Pin what the blank coercion does and does NOT swallow.

    It strips whitespace to None deliberately (a .env line of spaces is the same
    operator intent as a blank one). Everything else must still reach pydantic's
    own bool parsing, so a real value can never be silently discarded as "unset".
    """
    from aiko_gateway.config import Settings

    unset = Settings(_env_file=None).open_registration
    for blank in ("", " ", "   ", "\t", "\n"):
        assert Settings(open_registration=blank).open_registration == unset, (
            f"{blank!r} must behave as unset"
        )
    # Real values still win — the coercion must not eat them.
    for falsey in ("false", "False", "0", "no", "off"):
        assert Settings(open_registration=falsey).open_registration is False, falsey
    for truthy in ("true", "True", "1", "yes", "on"):
        assert Settings(open_registration=truthy).open_registration is True, truthy
    # PADDED real values must parse too (cage-match PR#141, Tesla — verified: before
    # the fix, a .env line reading `OPEN_REGISTRATION=false ` raised ValidationError
    # and crash-looped the island on the very field the coercion was added to save).
    for padded, expected in ((" false", False), ("false ", False), ("  false  ", False),
                             (" true", True), ("true ", True), ("\ttrue\n", True)):
        assert Settings(open_registration=padded).open_registration is expected, padded


# test_blank_is_unspecified_for_every_none_default_field DELETED (cage-match PR#141
# round 5, Tesla). Its post-construct predicate was `got.strip() == "" and got != ""`,
# which is "kept PADDING" — it could not see the very case it was named for, a field
# that keeps `""` instead of restoring absence. Rather than fix a third revision of a
# weaker check, note that it is SUBSUMED: test_whitespace_only_restores_absence_for_
# every_scalar_field already sweeps every forwarded field over ("", " ", "   ", "\t")
# and compares each against the field GENUINELY UNSET, which is the right reference
# and the stronger claim.

def test_not_forwarded_vars_are_not_interpolated():
    """An exclusion must be an INVERSE, not merely a skip.

    cage-match PR#141 (Tesla): `_NOT_FORWARDED` only exempts a var from the
    interpolation REQUIREMENT — it never asserts the var is actually static or
    absent. So `HOST: ${HOST:-0.0.0.0}` while `HOST` sits in `_NOT_FORWARDED` under
    the reason "container-internal bind, compose owns the port mapping" would pass
    invariant 7b AND the staleness check, while making the bind operator-tunable —
    the exact silent override the exclusion claims to forbid. The lattice was
    skip-not-inverse on this coil.
    """
    import re

    env = _chat_island_environment()
    contradictions = []
    for var in sorted(_NOT_FORWARDED):
        if var in env and re.search(r"\$\{" + re.escape(var) + r"[-:}]", env[var]):
            contradictions.append(f"{var} = {env[var]!r}")
    assert not contradictions, (
        "These vars are listed in _NOT_FORWARDED (i.e. deliberately NOT "
        f"operator-settable) yet compose interpolates the host value anyway: "
        f"{contradictions}. The exclusion is claiming one thing while the template "
        "does the opposite — either drop the exclusion or make the value static."
    )


def test_whitespace_only_restores_absence_for_every_scalar_field():
    """A whitespace-only value must never crash a box, whatever the field's type.

    cage-match PR#141 round 2 (Tesla), verified before fixing: `RATE_LIMIT_ENABLED="   "`
    raised ValidationError. Round 1's coercion only handled None-defaults; round 2's
    only added bools. Both were per-case allowlists inside a PR about killing
    allowlists, and the second was falsified within one round — so the rule is now
    "whitespace-only means the operator said nothing", applied to every field.

    These bytes were INERT before this PR. Forwarding them is what connects a stray
    space in a host .env to the container for the first time, so the property has to
    hold across the whole field set, not the three fields someone thought to name.
    """
    import os
    from unittest import mock

    from aiko_gateway.config import Settings

    crashed = []
    for name, field in Settings.model_fields.items():
        # Only fields this compose actually forwards: a _NOT_FORWARDED var is static
        # in the template, so the container never receives a host value for it at all.
        if name.upper() in _NOT_FORWARDED:
            continue
        # COMPLEX fields (list/dict) are now IN SCOPE — the exclusion that used to sit
        # here was the acceptance criterion for claude-tasks#3358, and removing it is
        # the test. They were exempt because pydantic-settings JSON-decodes them inside
        # the ENV SOURCE, which runs BEFORE any model validator, so `AIKO_CHANNELS="   "`
        # raised SettingsError upstream of `_normalise_env_strings` and crash-looped the
        # island while the identical scalar case was handled. The rule now also lives in
        # the source layer (`_WhitespaceIsAbsence`), which is the only layer that can see
        # them, so every forwarded field is covered by one property rather than by a
        # scalar rule plus a documented hole.
        # The reference is the variable genuinely ABSENT from the environment — NOT
        # a plain Settings(_env_file=None), which in this harness already carries JWT_SECRET and
        # ISLAND_SIGNING_SEED. Comparing against harness-set values reported a
        # failure for exactly the behaviour we want: blanking a secret falls back to
        # the dev default, which _harden_for_production then refuses to boot on. The
        # claim is "blank behaves as unset", so unset is what it must be measured
        # against.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(name.upper(), None)
            try:
                absent = getattr(Settings(_env_file=None), name)
            except Exception:  # noqa: BLE001 — absent is itself invalid; skip
                continue
        for blank in ("", " ", "   ", "\t"):
            with mock.patch.dict(os.environ, {name.upper(): blank}, clear=False):
                try:
                    got = getattr(Settings(_env_file=None), name)
                except Exception as exc:  # noqa: BLE001
                    crashed.append(f"{name.upper()}={blank!r} -> {type(exc).__name__}")
                    continue
                if got != absent:
                    crashed.append(
                        f"{name.upper()}={blank!r} gave {got!r}, but the same field "
                        f"genuinely UNSET gives {absent!r}"
                    )
    assert not crashed, (
        "A whitespace-only environment value must behave exactly as if the operator "
        f"had set nothing, and these do not: {crashed}."
    )


def test_string_fields_keep_their_whitespace():
    """The absence rule must not become a blanket strip.

    APNS_PRIVATE_KEY is a PEM and GITHUB_CLIENT_SECRET is a credential — silently
    rewriting their leading/trailing bytes would be a config layer editing secrets.
    Only NON-string scalars (bool/int/enum), where whitespace cannot carry meaning,
    are stripped.
    """
    import os
    from unittest import mock

    from aiko_gateway.config import Settings

    with mock.patch.dict(os.environ, {"GATEWAY_DISPLAY_NAME": "  Padded  "}, clear=False):
        assert Settings(_env_file=None).island_display_name == "  Padded  ", (
            "a string field must not be silently stripped"
        )


def test_config_module_has_no_unreachable_code():
    """No statement may follow a `return` in the same block.

    cage-match PR#141 round 3 (Tesla) — the finding this test exists for. A surgical
    edit to _normalise_env_strings left the ENTIRE previous implementation sitting
    after `return data`: dead, unreachable, and invisible to all 1066 passing tests,
    because a test suite cannot execute code that never runs. It is a booby trap
    rather than a bug — the next edit that "tidies up the double return" re-arms the
    crash-loop the function exists to prevent.

    Structural checks are the only instrument that can see this: it fails differently
    from the tests around it, which is exactly why it catches what they cannot.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "aiko_gateway" / "config.py"
    tree = ast.parse(src.read_text())
    unreachable = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body[:-1]):
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                nxt = body[i + 1]
                unreachable.append(
                    f"{type(nxt).__name__} at line {nxt.lineno} follows a "
                    f"{type(stmt).__name__} at line {stmt.lineno}"
                )
    assert not unreachable, (
        f"config.py contains unreachable statements: {unreachable}. Dead code in a "
        "boot-time validator is a trap for the next reader, not a harmless leftover."
    )


def test_the_whole_rendered_compose_environment_boots():
    """Construct Settings from ALL compose defaults at once, not one at a time.

    cage-match PR#141 round 4 (Carnot MEDIUM). `test_every_compose_default_is_constructible`
    patches a single variable per iteration against the ambient environment, so it
    proves each default is individually parseable — not that the environment compose
    actually produces boots. Cross-field guards (the APNs half-set check, the
    production hardening chain) only fire on combinations, and the full-map render was
    verified by hand against both boxes rather than by anything that runs again.

    This applies every default simultaneously, in a CLEARED environment so an
    ambient test-harness value cannot mask a missing one, and asserts the model
    constructs. `clear=True` also means this exercises the stock-island case: an
    operator who sets nothing at all.
    """
    import os
    import re
    from unittest import mock

    from aiko_gateway.config import Settings

    env = _chat_island_environment()
    rendered = {}
    for var, value in env.items():
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", str(value), re.S)
        if match:
            rendered[var] = match.group(1)
        elif not str(value).startswith("${"):
            rendered[var] = str(value)  # static literal
    # The two operator-supplied secrets get stand-ins. Both MUST be set on a real
    # island and the stock defaults are deliberately unusable in production:
    # JWT_SECRET uses the required `${VAR:?}` form so compose refuses to render at all
    # without it, and ISLAND_SIGNING_SEED's compose default is config._DEV_ISLAND_SEED,
    # which _harden_for_production rejects by design. This test asserts the rendered
    # environment boots for an operator who supplied their secrets and NOTHING else —
    # not that a secretless island boots, which must fail and does.
    #
    # Worth noting the seed guard only fired once every default was applied TOGETHER;
    # the per-variable test cannot reach a cross-field check like it.
    import base64 as _b64

    # Assigned, NOT setdefault: ISLAND_SIGNING_SEED IS in the rendered map, carrying
    # the dev seed as its compose default, so setdefault silently left the rejected
    # value in place. JWT_SECRET is absent from the map (its `${VAR:?}` form is not a
    # default), but assign both the same way so the two secrets cannot drift apart.
    rendered["JWT_SECRET"] = "x" * 40
    rendered["ISLAND_SIGNING_SEED"] = (
        _b64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    )
    # ...and one real operator CHOICE. A stock island with no sign-in ingress is
    # deliberately un-bootable in production (it could authenticate existing accounts
    # but never onboard anyone), so the minimum viable contract is "secrets + an
    # ingress" — which is exactly what both live boxes set. Asserting a
    # choice-free island boots would be asserting the opposite of the design.
    #
    # Adding these one at a time as production guards fired is itself the finding:
    # each is a REQUIREMENT of a production island that no per-variable test can see,
    # because every one of them is a cross-FIELD check. This dict is therefore the
    # minimum operator contract, written down — the same set both live boxes supply.
    rendered["PASSKEY_ENABLED"] = "true"          # a sign-in ingress must exist
    rendered["MODERATOR_USER_IDS"] = '["operator-1"]'  # moderator mode needs a moderator
    rendered["CSAM_RUNBOOK_ACKNOWLEDGED"] = "true"     # A5 operator acknowledgement
    rendered["GATEWAY_ID"] = "probe.example.org"       # per-island identity
    # `_env_file=None` is load-bearing, not tidiness (cage-match PR#141 round 7,
    # Tesla). Settings is configured `env_file=".env"` and this repo HAS a root .env,
    # so `clear=True` on os.environ does NOT isolate the test — pydantic-settings reads
    # the file regardless, and a developer's laptop config silently becomes part of
    # the "proof". That is a FOURTH state beyond repo / runtime / built-image, and it
    # was not in the census until Tesla put it there. Every Settings() in this file is
    # constructed the same way for the same reason.
    with mock.patch.dict(os.environ, rendered, clear=True):
        settings = Settings(_env_file=None)
    # Assert the VALUES the PR claims are behaviour-neutral, not merely that the model
    # constructed (Tesla: "behaviour-neutrality is prophesied, not measured"). These
    # four were previously read by hand off a box; now they are pinned in CI.
    assert settings.environment == "production"
    assert settings.open_registration is False, "prod must not self-serve register"
    assert settings.rate_limit_enabled is True, "the abuse cap must be on"
    assert settings.island_mode.value == "moderator"
    assert settings.max_request_bytes == 65536


def test_no_empty_compose_default_on_a_type_an_older_image_cannot_parse():
    """An empty compose default is only safe for string-like fields.

    cage-match PR#141 round 5 (Tesla) — the induction step that still manufactured the
    0.6.0 crash. `_render_default(None)` is `""`, so by the parity law the NEXT
    tri-state field added (`bool | None = None`) would legitimately want
    `${VAR:-}`. New-image constructibility greens it, because _normalise_env_strings
    deletes the blank. The PINNED image has no such coercion — that is precisely the
    crash-loop proven against ghcr.io/nickmeinhold/aiko-chat-island:0.6.0 — and
    compose cannot version-lock itself to the image, which is the whole reason
    OPEN_REGISTRATION carries `false` rather than blank.

    That lesson was recorded as a COMMENT on one field. This makes it a law: if the
    compose default is empty, the field's type must be one that accepts "" on an image
    WITHOUT the coercion — i.e. string-like. Anything else must state a real default.
    """
    import re

    from aiko_gateway.config import Settings

    env = _chat_island_environment()
    unsafe = []
    for name, field in Settings.model_fields.items():
        var = name.upper()
        if var in _NOT_FORWARDED or var not in env:
            continue
        match = re.fullmatch(r"\$\{" + re.escape(var) + r":-(.*)\}", env[var], re.S)
        if match is None or match.group(1) != "":
            continue
        # SAFE-LIST, not a danger-list (cage-match PR#141 round 7, Tesla). An earlier
        # revision reused production's _whitespace_is_never_meaningful, where False
        # means "unknown type, do not touch the bytes" — fail CLOSED on mutation. The
        # test read the same False as "this type can eat an empty string on 0.6.0" —
        # fail OPEN on a crash-loop. One helper, inverted safety: a shared blind spot
        # rather than shared code. Its complement walked straight through:
        # HttpUrl | None, UUID | None, datetime | None, Literal[...], Path.
        #
        # So enumerate what is SAFE and flag everything else. An unrecognised type is
        # treated as dangerous, which is the direction that cannot cause an outage.
        if not _accepts_empty_string_without_coercion(field.annotation):
            unsafe.append(f"{var} ({field.annotation})")
    assert not unsafe, (
        "These forwards use an EMPTY compose default on a field whose type cannot "
        f"parse the empty string: {unsafe}. On an island still running an older "
        "pinned image — one without config.py's blank-restores-absence coercion — "
        "that is a boot-time crash-loop, verified against 0.6.0. State the real "
        "default in compose instead (see OPEN_REGISTRATION, which carries `false` "
        "for exactly this reason)."
    )


# The exclusion set is FROZEN. Growing it is a security decision, so it takes two
# edits in two places and shows up twice in a diff.
_EXPECTED_EXCLUSIONS = frozenset({
    "HOST", "PORT", "JWT_ALGORITHM",
    "AIKO_MQTT_HOST", "AIKO_MQTT_PORT", "AIKO_NAMESPACE", "DB_URL", "ENVIRONMENT",
})


def test_the_exclusion_set_is_closed():
    """Resolves a real disagreement between two reviewers, rather than picking a side.

    Tesla (round 5) argued _MUST_FORWARD_ENV had to GO: a hand-maintained list of
    "vars that may never be excluded" is exactly the class this file kills, and this
    PR's own history proved it — round 3 added AUTH_RATE_LIMIT but not its window,
    round 4 patched that and still left the passkey and JWT TTLs ordinary. A partial
    list gives false assurance about the fields it forgot.

    Carnot (round 6) argued it had to STAY: without it, JWT_SECRET, ISLAND_SIGNING_SEED
    or PASSKEY_REQUIRE_USER_VERIFICATION can be moved into _NOT_FORWARDED behind any
    twenty characters of prose, since test_exemption_reasons_are_substantive measures
    length and not legitimacy. That reopens the class for the most sensitive settings.

    Both are right about different risks, so neither fix is correct. The asymmetry
    they missed: an "important vars" list is OPEN — unbounded, and you must remember
    to extend it. The EXCLUSION list is small and complete by construction, because it
    is precisely the complement of the totality check. So freeze the exclusions
    instead. Nothing can be quietly excluded (Carnot's risk), and there is no
    open-ended list of important names to forget (Tesla's risk). The duplication
    between this frozen set and _NOT_FORWARDED is deliberate: it is what makes
    widening the exemption impossible in one quiet edit.
    """
    actual = set(_NOT_FORWARDED)
    added = sorted(actual - _EXPECTED_EXCLUSIONS)
    removed = sorted(_EXPECTED_EXCLUSIONS - actual)
    assert not added, (
        f"New exclusion(s) from the forwarding invariant: {added}. Excluding a "
        "setting means an operator's host .env value silently does nothing — the "
        "class this whole file exists to prevent. If it is genuinely correct, add it "
        "to _EXPECTED_EXCLUSIONS too and say why in review; the second edit is the "
        "point, not an inconvenience."
    )
    assert not removed, (
        f"{removed} left _NOT_FORWARDED but is still in _EXPECTED_EXCLUSIONS. Drop it "
        "from the frozen set so the two agree."
    )


def test_a_custom_env_file_override_is_honoured(tmp_path):
    """`settings_customise_sources` re-classes the GIVEN source instances instead of
    building new ones, precisely so per-instance overrides survive. 72 tests already
    exercise `_env_file=None`; this pins the other half — an explicit PATH — because a
    reconstructed source would silently take `env_file` from model_config (".env") and
    read the repo's file instead of this one (cage-match PR#148, Carnot).

    Uses `island_display_name`: a plain str with a default, no validator, no
    cross-field guard, and absent from the test process env (so the env source cannot
    answer and the dotenv source must). Earlier drafts reached for AIKO_CHANNELS and
    then APNS_TOPIC and went red on JSON decoding and on the half-configured guard
    respectively — right answer, wrong failure mode, and a test with two ways to fail
    proves neither.

    Fails loudly if anyone "tidies" the re-classing into a fresh-instance construction.
    """
    from aiko_gateway.config import Settings

    env = tmp_path / "custom.env"
    env.write_text("GATEWAY_DISPLAY_NAME=from-the-custom-file\n")
    s = Settings(_env_file=str(env))
    assert s.island_display_name == "from-the-custom-file", (
        f"the custom _env_file was not read; got {s.island_display_name!r} — the "
        "source was probably reconstructed from model_config instead of re-classed"
    )


# --- legacy GATEWAY_* identity adoption (docs/island-vs-gateway.md) --------------
#
# GATEWAY_ID / GATEWAY_DISPLAY_NAME / GATEWAY_SEED_PEERS named the ISLAND, not the
# gateway edge, so they became ISLAND_*. Both names are forwarded by compose during
# the cutover window and resolved by Settings._adopt_legacy_gateway_identity.
#
# THESE ARE REGRESSION TESTS FOR A MEASURED NEAR-MISS, not speculative coverage.
# The obvious implementation — pydantic's AliasChoices("ISLAND_ID", "GATEWAY_ID") —
# is WRONG here, and silently: compose forwards an unset var as the EMPTY STRING,
# AliasChoices takes the first key PRESENT, so `ISLAND_ID: ${ISLAND_ID:-}` always
# wins and is always empty. Both live islands, whose .env still says GATEWAY_ID,
# would have booted with no identity — which fails the LiveKit remote-SFU boot guard
# and drops directory self-identity to a host-derived id. The absence-aware source
# does not rescue it either: returning None means "this source has no value", not
# "try the next alias". Hence two real fields and an explicit resolver.

def _settings_with(**env):
    import os
    from unittest import mock

    from aiko_gateway.config import Settings

    with mock.patch.dict(os.environ, env, clear=False):
        return Settings(_env_file=None)


_SEED = '[{"id":"x","display_name":"X","base_url":"https://x.example"}]'


def test_legacy_gateway_id_is_adopted_when_canonical_is_unset():
    assert _settings_with(GATEWAY_ID="legacy-id").island_id == "legacy-id"


def test_canonical_island_id_wins_over_legacy():
    assert _settings_with(ISLAND_ID="new", GATEWAY_ID="legacy-id").island_id == "new"


def test_empty_canonical_does_not_shadow_a_live_legacy_identity():
    """THE regression: compose forwards an unset ISLAND_ID as "".

    If this goes red, every box still on a GATEWAY_* .env loses its identity on the
    next deploy — silently, because an empty id is a valid-looking value.
    """
    assert _settings_with(ISLAND_ID="", GATEWAY_ID="legacy-id").island_id == "legacy-id"
    assert _settings_with(ISLAND_ID="   ", GATEWAY_ID="legacy-id").island_id == "legacy-id"


def test_empty_canonical_does_not_shadow_legacy_display_name_or_seed_peers():
    assert _settings_with(
        ISLAND_DISPLAY_NAME="", GATEWAY_DISPLAY_NAME="Imagineering"
    ).island_display_name == "Imagineering"
    assert len(
        _settings_with(ISLAND_SEED_PEERS="", GATEWAY_SEED_PEERS=_SEED).island_seed_peers
    ) == 1


def test_adoption_respects_each_fields_own_whitespace_policy():
    """The resolver must not IMPOSE a policy; it inherits each field's.

    The two fields deliberately differ, and adoption has to preserve that:
      * island_id is stripped downstream by _harden_for_production, because it is
        matched by exact string (it namespaces LiveKit rooms across islands), and a
        padded id silently never matches — the moderator-id bug class in costume.
      * island_display_name is NOT stripped: it is a label, and
        test_string_fields_keep_their_whitespace forbids a config layer from
        rewriting string fields.
    So the resolver uses .strip() to decide EMPTINESS only, and assigns raw bytes;
    what happens after is each field's own business.
    """
    assert _settings_with(GATEWAY_ID="  padded  ").island_id == "padded"
    assert (
        _settings_with(GATEWAY_DISPLAY_NAME="  Padded  ").island_display_name
        == "  Padded  "
    )


def test_neither_name_set_gives_the_plain_defaults():
    """The NULL arm — without it the tests above could pass on a stuck value."""
    s = _settings_with(
        ISLAND_ID="", GATEWAY_ID="", ISLAND_DISPLAY_NAME="", GATEWAY_DISPLAY_NAME="",
        ISLAND_SEED_PEERS="", GATEWAY_SEED_PEERS="",
    )
    assert s.island_id == ""
    assert s.island_display_name == "Aiko"
    assert s.island_seed_peers == []


def test_canonical_names_work_alone_on_a_cut_over_box():
    s = _settings_with(ISLAND_ID="fresh", ISLAND_SEED_PEERS=_SEED)
    assert s.island_id == "fresh"
    assert len(s.island_seed_peers) == 1


def test_both_legacy_and_canonical_names_are_forwarded_by_compose():
    """The resolver is useless if the legacy var never reaches the container."""
    env = _chat_island_environment()
    for var in (
        "ISLAND_ID", "GATEWAY_ID",
        "ISLAND_DISPLAY_NAME", "GATEWAY_DISPLAY_NAME",
        "ISLAND_SEED_PEERS", "GATEWAY_SEED_PEERS",
    ):
        assert var in env, f"{var} is not forwarded into the chat-island container"
