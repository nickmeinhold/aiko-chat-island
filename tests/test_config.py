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
# demand a strong secret + a gateway_id namespace + wss + both-or-neither creds.

_LK_SECRET_STRONG = "livekit-prod-secret-32-bytes-plus!"  # 34 chars >= 32


def _prod_lk(**over):
    base = dict(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                passkey_enabled=True, livekit_api_key="APIabc123",
                livekit_api_secret=_LK_SECRET_STRONG, gateway_id="island-a", **_PROD_MOD)
    base.update(over)
    return Settings(**base)


def test_prod_livekit_fully_configured_boots():
    s = _prod_lk()
    assert s.livekit_api_key == "APIabc123" and s.gateway_id == "island-a"


def test_prod_livekit_without_gateway_id_raises():
    # Empty gateway_id on a shared SFU = fail-open cross-island room/identity collision.
    with pytest.raises(ValidationError):
        _prod_lk(gateway_id="")


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
    # The guard is skipped entirely when LiveKit is unconfigured — no gateway_id needed.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET,
                 passkey_enabled=True, **_PROD_MOD)
    assert s.livekit_api_key == "" and s.is_production is True


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
    # into the env, so a bare Settings() would read that, not the dev default.
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
_MUST_FORWARD_ENV = {
    "JWT_SECRET",                    # secret — token forgery root
    "ISLAND_SIGNING_SEED",           # secret — manifest signing trust-root
    "ISLAND_KEY_VERSION",            # island identity — rotation lifecycle
    "MODERATOR_USER_IDS",            # operator policy — the operator seat (#100)
    "MODERATION_ALERT_WEBHOOK_URL",  # operator config — alert sink (#91)
    "CSAM_RUNBOOK_ACKNOWLEDGED",     # A5 operator commitment — prod moderator-boot gate
    "SOCIAL_SIGNIN_ENABLED",         # operator policy
    "APPLE_CLIENT_IDS",              # per-island auth
    "GOOGLE_CLIENT_IDS",             # per-island auth
    "GITHUB_CLIENT_ID",              # per-island auth
    "GITHUB_CLIENT_SECRET",          # secret
    "PASSKEY_ENABLED",               # per-island auth
    "PASSKEY_RP_ID",                 # per-island auth (domain-scoped)
    "PASSKEY_EXTRA_ORIGINS",         # per-island auth (the original incident)
    "PASSKEY_ANDROID_CERT_SHA256",   # per-island auth (Play signing certs)
    "GATEWAY_BASE_URL",              # per-island identity
    "GATEWAY_ID",                    # per-island identity
    "GATEWAY_DISPLAY_NAME",          # per-island identity
    "AIKO_CHANNELS",                 # operator policy — bridged bus channels (#2555)
    "GATEWAY_SEED_PEERS",            # operator-curated federation
    "GATEWAY_BOOTSTRAP_PEERS",       # operator-curated federation (gossip fetch)
    "GATEWAY_GOSSIP_ENABLED",        # operator policy — gossip fetch gate
}


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


def test_operator_settable_vars_are_forwarded_in_compose():
    env = _chat_island_environment()
    # Each operator var must be a KEY under chat-island.environment (scoping) AND its
    # value must interpolate the host var `${VAR...}` (host-passthrough), not a static
    # literal that would leave the host .env inert.
    not_keyed = sorted(v for v in _MUST_FORWARD_ENV if v not in env)
    assert not not_keyed, (
        "docker-compose.yml chat-island.environment is missing operator-settable "
        f"var(s): {not_keyed}. A host .env value is INERT unless the service forwards "
        "it. See invariant 7."
    )
    # Require `${VAR` followed by an interpolation boundary (`:`, `-`, or `}`), so a
    # value forwarding a DIFFERENT host var whose name merely starts with this one
    # (`JWT_SECRET: ${JWT_SECRET_EXTRA:-x}`) does NOT false-pass as forwarding VAR
    # (cage-match PR#110, Tesla). re.escape guards names with regex metachars.
    import re

    not_passthrough = sorted(
        v
        for v in _MUST_FORWARD_ENV
        if not re.search(r"\$\{" + re.escape(v) + r"[-:}]", env[v])
    )
    assert not not_passthrough, (
        "docker-compose.yml forwards these as a STATIC value (or a different host "
        f"var), not a host interpolation of the var itself: {not_passthrough}. Each "
        "must be `${VAR...}` so the operator's host .env value reaches the container; "
        "a literal is inert. See invariant 7."
    )


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
