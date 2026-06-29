"""Fail-closed configuration guards (prod auth hardening, task #38).

Two invariants:
  1. A production-like deployment MUST NOT boot with the dev-default jwt_secret —
     `Settings()` raises rather than serving forgeable tokens.
  2. Open self-registration defaults OFF in production and ON in dev, with an
     explicit override either way.

`_env_file=None` disables the repo `.env` so these tests exercise the code
defaults, not whatever a local `.env` happens to set.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiko_gateway.config import _DEV_JWT_SECRET, Settings


# --- invariant 1: fail-closed jwt_secret ------------------------------------

def test_prod_with_default_secret_raises():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret=_DEV_JWT_SECRET)


_STRONG_SECRET = "a-real-32-byte-minimum-secret-value"  # 35 chars >= 32


def test_prod_with_real_secret_boots():
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET)
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
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET)
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
    s = Settings(_env_file=None, environment="production",
                 jwt_secret=_STRONG_SECRET,
                 github_client_id="gh-id", github_client_secret="gh-secret")
    assert s.github_client_id == "gh-id"


def test_prod_broker_neither_set_boots():
    # No broker provider configured at all is fine — the provider is simply absent.
    s = Settings(_env_file=None, environment="production", jwt_secret=_STRONG_SECRET)
    assert s.github_client_id == ""


def test_dev_broker_half_config_boots():
    # Dev stays frictionless — the XOR guard is prod-only (a half-config dev
    # provider simply lists/behaves as not-configured at runtime, fail-closed).
    s = Settings(_env_file=None, environment="dev", github_client_id="gh-id")
    assert s.github_client_id == "gh-id"
