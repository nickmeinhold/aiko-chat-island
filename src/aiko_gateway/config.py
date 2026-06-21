"""Gateway configuration (pydantic-settings).

All values come from the environment (a `.env` file in dev; SOPS-generated env
in deploy). The aiko_services library reads AIKO_MQTT_* / AIKO_NAMESPACE from the
environment directly, so we surface them here as settings AND ensure they are
present in os.environ before any aiko import composes a process.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- aiko bus connection (consumed by aiko_services via os.environ) ---
    aiko_mqtt_host: str = "localhost"
    aiko_mqtt_port: int = 1883
    aiko_namespace: str = "aiko"

    # --- which aiko channel(s) the gateway bridges (Phase 1: just "general") ---
    aiko_channels: list[str] = ["general"]

    # --- database ---
    db_url: str = "postgresql+asyncpg://aiko:dev@localhost:5433/aiko_chat"

    # --- HTTP server ---
    host: str = "127.0.0.1"
    port: int = 8095

    def export_aiko_env(self) -> None:
        """Ensure aiko_services sees our MQTT config. Must run before composing
        any aiko process (aiko reads these from os.environ at import/compose)."""
        import os
        os.environ.setdefault("AIKO_MQTT_HOST", self.aiko_mqtt_host)
        os.environ.setdefault("AIKO_MQTT_PORT", str(self.aiko_mqtt_port))
        os.environ.setdefault("AIKO_NAMESPACE", self.aiko_namespace)


settings = Settings()
