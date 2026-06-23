"""Gateway configuration (pydantic-settings).

All values come from the environment (a `.env` file in dev; SOPS-generated env
in deploy). The aiko_services library reads AIKO_MQTT_* / AIKO_NAMESPACE from the
environment directly, so we surface them here as settings AND ensure they are
present in os.environ before any aiko import composes a process.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The dev-only JWT secret. Single source so the default and the fail-closed
# guard below can never disagree (a prod boot with THIS value is rejected).
_DEV_JWT_SECRET = "dev-insecure-change-me"

# Environments treated as non-production. Anything else (incl. unknown values
# AND the absence of ENVIRONMENT, which defaults to "production" below) is
# production-like — fail-closed: forgetting to declare the environment hardens
# rather than relaxes the guards.
_NON_PROD_ENVIRONMENTS = frozenset({"dev", "development", "test", "local"})

# Minimum production JWT secret length. Matches PyJWT's HS256 recommendation
# (>= 32 bytes); a denylist on the exact dev default is a sieve, so prod also
# requires a strong secret (allowlist), not merely "not the dev value".
_MIN_PROD_SECRET_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Deployment environment. Defaults to "production" so a deploy that FORGETS
    # to set ENVIRONMENT still arms the fail-closed guards (absence = unsafe ⇒
    # treat as prod). Local dev / CI must EXPLICITLY declare a non-prod value
    # (ENVIRONMENT=dev in .env; ENVIRONMENT=test in the test harness).
    environment: str = "production"

    # --- aiko bus connection (consumed by aiko_services via os.environ) ---
    aiko_mqtt_host: str = "localhost"
    aiko_mqtt_port: int = 1883
    aiko_namespace: str = "aiko"

    # --- which aiko channel(s) the gateway bridges (Phase 1: just "general") ---
    aiko_channels: list[str] = ["general"]

    # --- database ---
    db_url: str = "postgresql+asyncpg://aiko:dev@localhost:5433/aiko_chat"

    # --- auth (JWT) ---  dev default; deploy supplies via SOPS.
    jwt_secret: str = _DEV_JWT_SECRET
    # Symmetric HMAC only — constrained so an env override can't introduce an
    # asymmetric/none-alg downgrade on an auth-critical setting.
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_access_ttl_seconds: int = 15 * 60        # 15 min
    jwt_refresh_ttl_seconds: int = 30 * 24 * 3600  # 30 days

    # Self-service registration. None → resolved by environment in the validator
    # (open in dev, closed in prod); set OPEN_REGISTRATION to override either way.
    open_registration: bool | None = None

    # --- HTTP server ---
    host: str = "127.0.0.1"
    port: int = 8095

    @property
    def is_production(self) -> bool:
        """Anything outside the known non-prod allowlist is production-like.
        `.strip()` so accidental whitespace padding (ENVIRONMENT=' dev ') is
        still recognized as the intended env rather than mis-hardening."""
        return self.environment.strip().lower() not in _NON_PROD_ENVIRONMENTS

    @model_validator(mode="after")
    def _harden_for_production(self) -> "Settings":
        # Fail closed: a production boot must have a STRONG, non-default JWT
        # secret — otherwise anyone could mint valid tokens for any user_id.
        if self.is_production:
            secret = self.jwt_secret.strip()
            if self.jwt_secret == _DEV_JWT_SECRET:
                raise ValueError(
                    "jwt_secret is still the dev default in a production "
                    f"environment (environment={self.environment!r}). Refusing to "
                    "boot — supply a real JWT_SECRET (e.g. via SOPS)."
                )
            if len(secret) < _MIN_PROD_SECRET_LEN:
                raise ValueError(
                    f"jwt_secret is too weak for production "
                    f"(len={len(secret)} < {_MIN_PROD_SECRET_LEN}). Refusing to "
                    "boot — supply a JWT_SECRET of at least "
                    f"{_MIN_PROD_SECRET_LEN} chars."
                )
            # No break-glass for open registration in prod: with I2 membership
            # not yet enforced, an open prod /register lets any self-created
            # account read every channel. Forbid the override until I2 lands.
            if self.open_registration is True:
                raise ValueError(
                    "open_registration must not be enabled in production until "
                    "I2 membership is enforced (an open /register would expose "
                    "all channels to any self-registered user)."
                )
        # Resolve registration default by environment when not explicitly set:
        # open in dev, closed in prod.
        if self.open_registration is None:
            self.open_registration = not self.is_production
        return self

    def export_aiko_env(self) -> None:
        """Ensure aiko_services sees our MQTT config. Must run before composing
        any aiko process (aiko reads these from os.environ at import/compose)."""
        import os
        os.environ.setdefault("AIKO_MQTT_HOST", self.aiko_mqtt_host)
        os.environ.setdefault("AIKO_MQTT_PORT", str(self.aiko_mqtt_port))
        os.environ.setdefault("AIKO_NAMESPACE", self.aiko_namespace)


settings = Settings()
