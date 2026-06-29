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

    # --- social sign-in (#13: Apple + Google native ID-token flow) ---
    # Explicit on/off, default False, LOUD in prod (mirror open_registration's
    # explicit-default posture). Unlike open_registration, social sign-in MAY be
    # enabled in production (Nick's decision 2026-06-27): the SAME I2 risk applies
    # — until #36 membership lands, any signed-in user can read every channel —
    # but that tradeoff is ACCEPTED for the current early-users phase so the live
    # gateway is reachable at all. The risk is named here and in the PR, not
    # silently absorbed.
    social_signin_enabled: bool = False
    # The audience allowlist: OUR provider client IDs. A provider ID token's `aud`
    # must be one of these. PUBLIC values (native ID-token flow needs no client
    # secret), so plain config — NOT SOPS. EMPTY ⇒ the verifier rejects every
    # token (fail-closed): a token minted for any OTHER Apple/Google app must
    # never authenticate here.
    apple_client_ids: list[str] = []
    google_client_ids: list[str] = []
    # Provisioning token TTL: a brand-new social user gets a short-lived signed
    # token (NOT a DB row) to carry (provider, sub, suggested name/email) from the
    # verify step to the handle-claim step. Short window — it's a one-step
    # handoff, not a session.
    provisioning_ttl_seconds: int = 10 * 60  # 10 min

    # --- OAuth broker (#21: server-side authorization-code flow) ---
    # Increment 2 scope: the CORE broker flow + GitHub as the first provider.
    # Unlike the native ID-token flow (apple/google above), the broker performs
    # the authorization-code exchange SERVER-side, so it needs a confidential
    # client secret. These are SECRETS — supplied via the host .env (SOPS in
    # deploy), NEVER committed. A provider counts as "configured" only when BOTH
    # its id AND secret are set; either alone is a half-config that XOR-fails at
    # boot in prod (see _harden_for_production).
    github_client_id: str = ""
    github_client_secret: str = ""
    # The base URL of THIS gateway — used to derive the provider redirect_uri the
    # broker hands to the authorize endpoint (so the host is configured in ONE
    # place, not hardcoded across the start/callback handlers).
    gateway_base_url: str = "https://chat.imagineering.cc"
    # The app's Universal/App Link the browser is redirected back to after the
    # broker completes (carrying the handoff code, or an error indicator). This is
    # a FIXED config value — open-redirect defense: the final redirect target is
    # NEVER read from a request parameter, only from here.
    app_oauth_callback_url: str = "https://chat.imagineering.cc/applink/auth"
    # OAuth state token TTL (CSRF/integrity, the round-trip from /start to
    # /callback). Short — a human completing a provider consent screen.
    oauth_state_ttl_seconds: int = 10 * 60  # 10 min
    # Handoff code TTL: the window between the browser landing back on the app and
    # the app POSTing /exchange. Very short — a single immediate redemption.
    oauth_handoff_ttl_seconds: int = 2 * 60  # 2 min

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
            # Social sign-in IS permitted in prod (unlike open_registration), but
            # enabling it with NO client-ID allowlist is a guaranteed-broken
            # config: the verifier would reject every token (empty aud allowlist
            # = reject-all). Fail LOUD at boot rather than silently 401 every
            # real login — a prose "must configure client IDs" is not a guard.
            if self.social_signin_enabled and not (
                self.apple_client_ids or self.google_client_ids
            ):
                raise ValueError(
                    "social_signin_enabled is True in production but no "
                    "apple_client_ids / google_client_ids are configured. The "
                    "verifier would reject every token (empty audience allowlist "
                    "= reject-all). Refusing to boot — supply at least one "
                    "provider client ID."
                )
            # Broker providers: a partial (XOR) config — only ONE of id/secret —
            # is a latent footgun. The provider would appear "almost configured"
            # but the confidential token exchange needs BOTH, so it would fail at
            # the worst time (mid-login) with an opaque 4xx. Fail LOUD at boot
            # instead, naming the half that's missing. (Listing-as-configured
            # requires both, so a XOR provider is invisible AND broken — exactly
            # the silent-misconfig class config hardening exists to kill.)
            for slug, cid, secret in (
                ("github", self.github_client_id, self.github_client_secret),
            ):
                if bool(cid) != bool(secret):
                    missing = "client_secret" if cid else "client_id"
                    raise ValueError(
                        f"oauth broker provider {slug!r} is half-configured: "
                        f"{missing} is missing (the other half is set). The "
                        "confidential authorization-code exchange needs BOTH a "
                        "client_id and a client_secret. Refusing to boot — supply "
                        f"the missing {slug.upper()}_{missing.upper()} or unset "
                        "both to disable the provider."
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
