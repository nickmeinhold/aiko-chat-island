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


def test_prod_with_real_secret_boots():
    s = Settings(_env_file=None, environment="production",
                 jwt_secret="a-real-32-byte-minimum-secret-value")
    assert s.is_production is True


def test_dev_with_default_secret_boots():
    # The whole point of the dev default: local dev must stay frictionless.
    s = Settings(_env_file=None, environment="dev", jwt_secret=_DEV_JWT_SECRET)
    assert s.is_production is False


def test_unknown_environment_is_treated_as_production():
    # Fail-closed: an unrecognized environment is production-like, so the dev
    # default secret must still be rejected.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="staging", jwt_secret=_DEV_JWT_SECRET)


# --- invariant 2: registration gating ---------------------------------------

def test_open_registration_defaults_on_in_dev():
    s = Settings(_env_file=None, environment="dev")
    assert s.open_registration is True


def test_open_registration_defaults_off_in_prod():
    s = Settings(_env_file=None, environment="production",
                 jwt_secret="a-real-32-byte-minimum-secret-value")
    assert s.open_registration is False


def test_open_registration_explicit_override_in_prod():
    s = Settings(_env_file=None, environment="production",
                 jwt_secret="a-real-32-byte-minimum-secret-value",
                 open_registration=True)
    assert s.open_registration is True
