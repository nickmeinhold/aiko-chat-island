"""Gateway configuration (pydantic-settings).

All values come from the environment (a `.env` file in dev; SOPS-generated env
in deploy). The aiko_services library reads AIKO_MQTT_* / AIKO_NAMESPACE from the
environment directly, so we surface them here as settings AND ensure they are
present in os.environ before any aiko import composes a process.
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

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
    # Dev defaults to file-backed SQLite — the SAME engine prod runs (deploy sets
    # DB_URL=sqlite+aiosqlite:////data/aiko.db; the #1281 single-home thesis makes
    # SQLite the deployment target, not a stopgap). Dev-on-Postgres was legacy
    # drift from before that move and was blind to everything that actually ships:
    # SQLite single-writer locking ("database is locked"), type affinity, CHECK
    # quirks, and FK enforcement defaulting OFF (the gateway relies on
    # application-level cascades — see channels_service / accounts_service — NOT
    # ondelete=CASCADE, so the deploy dialect's behavior must be what dev exercises).
    # A relative path → an ./aiko_dev.db file in the working dir (gitignored).
    db_url: str = "sqlite+aiosqlite:///./aiko_dev.db"

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
    # Replay defense (#13). This flag is the SINGLE SWITCH for option-a (the
    # server-ISSUED single-use nonce). When True, /v1/auth/social (a) REFUSES a
    # request carrying no nonce (presence enforcement) AND (b) requires the supplied
    # nonce to be one the gateway issued + not yet consumed (the consume; #1491
    # gates this on the flag, not on nonce presence). When False (default, today's
    # app) BOTH are off: a request without a nonce is accepted, and a supplied nonce
    # is NOT consumed — so option-a's captured-request replay closure is inert even
    # for a client that opted into POST /v1/auth/nonce (cage-match PR#43). Default
    # False because the live app does not send a SERVER-issued nonce yet — it sends
    # its OWN nonce for option-b PROVIDER binding, verified inside verify_id_token.
    # Independent of verification: a WRONG nonce is ALWAYS rejected by verify_id_token
    # regardless of this flag (option-b). Flip to True as the final step of the
    # staged rollout, once the app ships POST /v1/auth/nonce (#1449).
    social_nonce_required: bool = False
    # TTL for a server-ISSUED single-use nonce (#13 option (a)): the window between
    # the app calling POST /v1/auth/nonce and POSTing /v1/auth/social with the
    # provider id_token bound to it. Short — a human completing a Sign-in-with-Apple
    # / Google sheet, no longer.
    social_nonce_ttl_seconds: int = 10 * 60  # 10 min
    # The audience allowlist: OUR provider client IDs. A provider ID token's `aud`
    # must be one of these. PUBLIC values (native ID-token flow needs no client
    # secret), so plain config — NOT SOPS. EMPTY ⇒ the verifier rejects every
    # token (fail-closed): a token minted for any OTHER Apple/Google app must
    # never authenticate here.
    apple_client_ids: list[str] = []
    google_client_ids: list[str] = []
    # Site-wide moderators (Piece B, #7). User ids (ULIDs) that may act on the
    # report queue: view pending reports, take a message down, dismiss a report,
    # ban/unban a user. Fail-closed empty — an island with no configured moderator
    # has no one who can reach the moderation endpoints (require_moderator 403s
    # everyone). JSON array in the env (MODERATOR_USER_IDS), mirroring
    # apple_client_ids. Config, not a role system: full role machinery (invite
    # co-mods, audit log) is deferred until there's a second moderator.
    moderator_user_ids: list[str] = []
    # Agent-admin BOOTSTRAP set (Citizenship H1, #2403). User ids (ULIDs) that are
    # seeded as agent-admins at boot — the ONE-TIME chicken-and-egg solver for the
    # first-class, DB-backed `users.is_agent_admin` role (which the agent-binding gate
    # actually reads). This is NOT the allowlist the gate consults: the gate reads the
    # persisted flag, granted/revoked at runtime by an existing agent-admin
    # (agents_service.grant_agent_admin / revoke_agent_admin). At startup
    # agents_service.reconcile_bootstrap_agent_admins grants the flag to every listed
    # id that exists and does not yet have it — ADDITIVE ONLY (it never revokes), so a
    # runtime grant survives a restart and removing an id here does NOT demote a live
    # admin (use the revoke path). Minting a production agent identity is a HIGHER-
    # privilege act than content moderation (it forges a first-class OIDC citizen), so
    # this role is its OWN capability and never reuses moderator_user_ids — the
    # cage-match flagged that reuse. Fail-closed empty: with no seeded admin AND no
    # runtime grant, NOBODY can mint a binding (require_agent_binding_admin 403s
    # everyone). JSON array in the env AGENT_ADMIN_BOOTSTRAP_IDS (pydantic-settings
    # derives the env name from the field name, like MODERATOR_USER_IDS above).
    agent_admin_bootstrap_ids: list[str] = []
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
    # place, not hardcoded across the start/callback handlers). ALSO this gateway's
    # advertised base_url in the peer directory (#1546).
    gateway_base_url: str = "https://chat.imagineering.cc"

    # --- island/gateway directory via peer gossip (#1546) ---
    # The DECENTRALIZED discovery layer: each gateway advertises a known-peer set
    # and converges by anti-entropy gossip — NO central registry. See
    # domain/peers_service.py + rest/islands.py. The app's server picker calls
    # GET /v1/islands (deprecated alias /v1/gateways) to swap its hardcoded preset list.
    #
    # This gateway's stable id in the directory. Empty → derived from the
    # gateway_base_url host (so a single-gateway deploy still self-identifies).
    gateway_id: str = ""
    # Human label the picker shows for THIS gateway.
    gateway_display_name: str = "Aiko"
    # Operator-curated static peers: FULL entries merged into the directory at
    # startup with NO network fetch. Authentic BY CONSTRUCTION (the operator put
    # them here) — this IS the "operator allowlist" the peers_service trust banner
    # names as the real anti-poisoning defense, and for a handful of islands it makes
    # gossip unnecessary: each island lists the others directly, no SSRF-prone fetch.
    # JSON array of {"id","display_name","base_url"}. Preferred over gossip until
    # transitive discovery (3+ islands) actually justifies the fetch path.
    gateway_seed_peers: list[dict] = []
    # Bootstrap contacts: peer gateway base URLs to GOSSIP with (fetched at startup).
    # Only used when gossip is enabled. A known-node seed (P2P bootstrap), NOT a
    # central registry — each island just needs one reachable peer to converge.
    gateway_bootstrap_peers: list[str] = []
    # Fail-closed gate on the anti-entropy FETCH path. Gossip pulls attacker-
    # influenceable peer base URLs (SSRF surface — address-class filtering not yet
    # implemented; see #1578), so it stays OFF unless explicitly enabled. With it
    # off, the directory still serves self + seed_peers (no fetch). Enable only once
    # the SSRF/OOM hardening lands AND transitive discovery is actually needed.
    gateway_gossip_enabled: bool = False
    # How often the background gossip loop pulls each known peer's island directory and
    # merges. Takes effect only when gateway_gossip_enabled is true.
    gateway_gossip_interval_seconds: int = 300
    # The app's Universal/App Link the browser is redirected back to after the
    # broker completes (carrying the handoff code, or an error indicator). This is
    # a FIXED config value — open-redirect defense: the final redirect target is
    # NEVER read from a request parameter, only from here.
    app_oauth_callback_url: str = "aikochat://auth"
    # OAuth state token TTL (CSRF/integrity, the round-trip from /start to
    # /callback). Short — a human completing a provider consent screen.
    oauth_state_ttl_seconds: int = 10 * 60  # 10 min
    # Handoff code TTL: the window between the browser landing back on the app and
    # the app POSTing /exchange. Very short — a single immediate redemption.
    oauth_handoff_ttl_seconds: int = 2 * 60  # 2 min

    # --- WebAuthn passkeys (#1471) ---
    # Passwordless credential sign-in. Endpoints can deploy DARK; the feature stays
    # invisible to the app until passkey_enabled flips the /providers advertisement
    # on (the handoff's "deploy endpoints first, advertise last" rollout).
    passkey_enabled: bool = False
    # The Relying Party ID — the registrable domain the credential is scoped to.
    # MUST equal the host the app presents; a credential is bound to this rp_id and
    # unusable elsewhere. The web expected-origin is DERIVED from it (https://<id>).
    passkey_rp_id: str = "chat.imagineering.cc"
    passkey_rp_name: str = "Aiko Chat"
    # EXTRA expected origins beyond the derived web origin. A native app does NOT
    # present a single browser origin: iOS presents the web origin https://<rp_id>
    # (derived, always allowed); Android (Credential Manager) presents an
    # android:apk-key-hash:<base64url-sha256-of-Play-signing-cert> origin, which is
    # unknown until Play App Signing is registered (app task #20) — so it is
    # supplied HERE when known. Empty until then (iOS still works; Android blocked).
    passkey_extra_origins: list[str] = []
    # WebAuthn ceremony challenge TTL — the round-trip from start to finish (a user
    # tapping their authenticator). Short, single-use.
    passkey_challenge_ttl_seconds: int = 5 * 60  # 5 min
    # Require USER VERIFICATION (biometric/PIN), not just user presence. A passkey
    # is a PASSWORDLESS PRIMARY factor, so the default is True (cage-match #38,
    # Carnot HIGH): without it a stolen UNLOCKED device could authenticate on
    # possession alone. Drives both the ceremony request (REQUIRED vs PREFERRED) and
    # the finish-time assertion check. Platform authenticators (iOS/Android) always
    # do UV, so REQUIRED does not lock them out; flip to False only if a target
    # authenticator class genuinely can't do UV and possession-only is accepted.
    passkey_require_user_verification: bool = True
    # Domain-association files served at /.well-known/* so iOS/Android trust the app
    # to use passkeys on this domain. App identifiers from the merged app config
    # (PR#38) — public, not secrets. Served always (the app verifies association
    # BEFORE passkey_enabled flips advertisement on).
    passkey_ios_app_id: str = "SPL85G447K.cc.imagineering.aikoChatApp"
    passkey_android_package: str = "cc.imagineering.aiko_chat_app"
    # Android Digital Asset Links needs the PLAY APP SIGNING SHA-256 (the cert
    # Google re-signs with) — unknown until Play signing is registered (app task
    # #20). Empty until then: assetlinks serves an empty fingerprint list (Android
    # App Links won't verify yet; the iOS AASA is unaffected). Configure when known.
    passkey_android_cert_sha256: list[str] = []

    # --- social recovery (Design 05: guardian approval quorum) ---
    # The time-locked veto window: after a valid guardian quorum opens a pending
    # recovery, existing devices have this long to cancel before the client can
    # finalize (re-bind the new passkey). 72h default — the one genuine product-feel
    # dial (Design 05 §11 open question 1). Per-island env-overridable
    # (RECOVERY_VETO_WINDOW_SECONDS) like every other setting. The deadline is server
    # wall-clock and WRITE-ONCE at pending-row birth; changing this value affects only
    # NEW recoveries, never advances a live deadline.
    recovery_veto_window_seconds: int = 72 * 3600  # 72h

    # --- abuse limits (#28) ---
    # Per-client rate limit on the public auth ceremonies (passkey/social/oauth/
    # register/login/nonce). A blast-radius cap on unauthenticated, sometimes
    # crypto-expensive or account-creating endpoints — NOT an authn control. Keyed
    # by client IP (X-Forwarded-For rightmost, behind Caddy; see rate_limit.py),
    # per endpoint-bucket. Generous enough that a real client doing a full ceremony
    # + retries never trips it. In-process fixed window (single worker; see module).
    rate_limit_enabled: bool = True
    auth_rate_limit: int = 20  # requests per window, per IP, per bucket
    auth_rate_limit_window_seconds: int = 60
    # Reject request bodies larger than this with 413 (app-wide middleware). Auth
    # payloads (WebAuthn, id_token JWTs) are a few KB and chat messages small text;
    # there is no upload endpoint, so this never trips a legitimate request.
    max_request_bytes: int = 64 * 1024  # 64 KiB

    # --- moderation / ops ---
    # Best-effort operator alert: when set, creating a message report fires a
    # fire-and-forget POST to this URL so the island operator is pinged the moment
    # abuse is flagged (channel-agnostic — point it at a Telegram bot / ntfy / a
    # Worker). None → no-op (the default; existing behavior byte-for-byte
    # unchanged). The destination comes ONLY from here, never from a request, so it
    # carries no SSRF surface; only the payload carries user content. Delivery is
    # non-blocking (BackgroundTasks) and swallows every failure — a broken webhook
    # never affects the report write. See rest/moderation.py.
    moderation_alert_webhook_url: str | None = None

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
            broker_configured = bool(
                self.github_client_id and self.github_client_secret)
            if self.social_signin_enabled and not (
                self.apple_client_ids or self.google_client_ids
                or broker_configured
            ):
                raise ValueError(
                    "social_signin_enabled is True in production but no usable "
                    "provider is configured. Supply at least one of: "
                    "apple_client_ids / google_client_ids (native ID-token flow), "
                    "or a fully-configured broker provider (e.g. both "
                    "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET). With none, the "
                    "native verifier would reject every token (empty audience "
                    "allowlist = reject-all) and no broker provider would be "
                    "offered. Refusing to boot."
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
            # At least one viable NEW-USER ingress must exist in production.
            # open_registration is force-closed above (I2 unenforced), so the ONLY
            # ways a new account comes into existence in prod are passkey
            # registration or social sign-in. With BOTH off, the island can still
            # authenticate pre-existing password accounts via /login but can never
            # ONBOARD anyone — a locked, un-joinable deployment. This is the
            # "retirement is one env line from being false" footgun (#1927):
            # retiring social (#1923) BEFORE enabling passkey silently bricks
            # onboarding. Fail closed — same both-or-neither discipline as the
            # broker XOR guard above; the operator just flips one flag. Relying on
            # social_signin_enabled alone is sound: the social guard above already
            # refused boot if social is on without a usable provider, so the flag
            # here implies an advertised, usable social ingress.
            #
            # Both arms are lint-checked upstream, but with different reach: social
            # gets a full provider-completeness guard (social_signin_enabled ⟹ a
            # usable provider), while passkey gets only a BEST-EFFORT rp_id sanity
            # check (see the passkey block just below) — it catches an obviously-bad
            # rp_id but CANNOT confirm a working ceremony from Settings alone. So this
            # invariant's honest guarantee is "at least one ingress is ENABLED and not
            # obviously-misconfigured", not "a registration will succeed". That
            # enabled-check is itself complete and is the real value here. When a THIRD
            # legitimate prod ingress lands
            # (open_registration/invites after I2 membership, #36), this predicate
            # MUST be extended in the same change or a joinable-by-policy island
            # would refuse boot — see the follow-up task.
            #
            # Passkey RP-ID SANITY (prod) — a BEST-EFFORT lint, NOT a guarantee the
            # ceremony works. Honest scope (cage-match PR#97, Carnot + Tesla): Settings
            # cannot see the REAL serving host, so config self-consistency != config
            # correctness. If gateway_base_url AND passkey_rp_id are BOTH left at
            # defaults that don't match the true host, this cannot detect it — the two
            # defaults agree with each other while disagreeing with reality. (We can't
            # "require non-default" either: imagineering legitimately runs on the
            # defaults.) The COMPLETE guarantee is invariant 5 (an ingress is ENABLED);
            # this is a lint on top of it. What the lint DOES catch:
            #   (a) an rp_id that doesn't match the CONFIGURED host — e.g. base_url set
            #       to this island but rp_id left at another island's default (a common,
            #       real operator slip); and
            #   (b) an obviously-invalid single-label / public-suffix rp_id (e.g. "com")
            #       that no browser will scope a credential to.
            # A passkey credential is bound to rp_id and is usable only where the origin
            # host equals rp_id or is a subdomain of it (the WebAuthn rp_id rule).
            # KNOWN RESIDUAL: multi-label public suffixes ("co.uk") need the Public
            # Suffix List to reject and are NOT caught here (no PSL dependency) — #51.
            if self.passkey_enabled:
                host = (urlparse(self.gateway_base_url).hostname or "").strip().lower()
                rp = self.passkey_rp_id.strip().lower()
                rp_single_label = "." not in rp  # "com", "localhost": never a valid prod RP
                if (not rp or not host or rp_single_label
                        or not (host == rp or host.endswith("." + rp))):
                    raise ValueError(
                        f"passkey_enabled is True in production but passkey_rp_id "
                        f"({self.passkey_rp_id!r}) is not a usable Relying Party ID for this "
                        f"gateway's host ({host!r}, from gateway_base_url="
                        f"{self.gateway_base_url!r}). It must be a multi-label domain that the "
                        "serving host equals or is a subdomain of (the WebAuthn rp_id rule; a "
                        "single-label or public-suffix rp_id like 'com' is rejected by browsers). "
                        "Refusing to boot — set PASSKEY_RP_ID to this gateway's registrable "
                        "domain (typically the host in GATEWAY_BASE_URL). NOTE: this is a "
                        "best-effort check; it cannot confirm the rp_id matches the REAL serving "
                        "host when gateway_base_url is also left at a default."
                    )
            if not (self.passkey_enabled or self.social_signin_enabled):
                raise ValueError(
                    "no viable sign-in ingress is configured for production: both "
                    "passkey (PASSKEY_ENABLED) and social sign-in "
                    "(SOCIAL_SIGNIN_ENABLED) are disabled, and self-registration "
                    "is closed in production until I2 membership is enforced. The "
                    "island could authenticate pre-existing accounts but could "
                    "never onboard a new user (a locked, un-joinable deployment). "
                    "Refusing to boot — enable at least one of PASSKEY_ENABLED or "
                    "SOCIAL_SIGNIN_ENABLED (with a configured provider)."
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
