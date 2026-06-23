"""Gateway configuration (pydantic-settings).

All values come from the environment (a `.env` file in dev; SOPS-generated env
in deploy). The aiko_services library reads AIKO_MQTT_* / AIKO_NAMESPACE from the
environment directly, so we surface them here as settings AND ensure they are
present in os.environ before any aiko import composes a process.
"""
from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The dev-only JWT secret. Single source so the default and the fail-closed
# guard below can never disagree (a prod boot with THIS value is rejected).
_DEV_JWT_SECRET = "dev-insecure-change-me"

# Environments treated as non-production. Anything else (incl. unknown values)
# is production-like — fail-closed: an unrecognized env hardens rather than
# relaxes the guards.
_NON_PROD_ENVIRONMENTS = frozenset({"dev", "development", "test", "local"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Deployment environment. "dev" (default) keeps local frictionless; deploy
    # sets ENVIRONMENT=production, which arms the fail-closed guards below.
    environment: str = "dev"

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
    jwt_algorithm: str = "HS256"
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
        """Anything outside the known non-prod allowlist is production-like."""
        return self.environment.lower() not in _NON_PROD_ENVIRONMENTS

    @model_validator(mode="after")
    def _harden_for_production(self) -> "Settings":
        # Fail closed: a production boot must never use the dev-default secret —
        # it would let anyone mint valid tokens for any user_id.
        if self.is_production and self.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError(
                "jwt_secret is still the dev default in a production environment "
                f"(environment={self.environment!r}). Refusing to boot — supply a "
                "real JWT_SECRET (e.g. via SOPS)."
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
