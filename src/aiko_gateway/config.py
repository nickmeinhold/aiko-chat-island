"""Gateway configuration (pydantic-settings).

All values come from the environment (a `.env` file in dev; SOPS-generated env
in deploy). The aiko_services library reads AIKO_MQTT_* / AIKO_NAMESPACE from the
environment directly, so we surface them here as settings AND ensure they are
present in os.environ before any aiko import composes a process.
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Leaf import (stdlib-only enum) — safe at module top, no config<->domain cycle. The
# SINGLE source of truth for the mode vocabulary, shared with the signing codec so the
# config field and the manifest verifier can never drift.
from .domain.island_mode import IslandMode

# The dev-only JWT secret. Single source so the default and the fail-closed
# guard below can never disagree (a prod boot with THIS value is rejected).
_DEV_JWT_SECRET = "dev-insecure-change-me"

# The dev-only island signing seed (unpadded base64url of the 32 bytes
# b"aiko-dev-island-seed-DO-NOT-USE!"). Same posture as _DEV_JWT_SECRET: dev boots
# on it frictionlessly; a production boot with THIS value is rejected (a real island
# identity key must be operator-supplied via SOPS). Single source so the default and
# the fail-closed guard can never disagree.
_DEV_ISLAND_SEED = "YWlrby1kZXYtaXNsYW5kLXNlZWQtRE8tTk9ULVVTRSE"

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

    # --- LiveKit (video/audio transport) ---
    # The island is the AUTHORIZER of who may join a LiveKit room and with what
    # powers; it mints short-lived HS256 join tokens and never proxies media. The
    # SFU is self-hosted (imagineering); clients connect to `livekit_url` directly.
    # Video is an OPTIONAL capability: absent api key/secret => the endpoint returns
    # 503, it does NOT fail-closed the boot (unlike jwt_secret / island seed, which
    # are core). A misconfigured secret only yields tokens LiveKit itself rejects —
    # no data-plane risk — so endpoint-level 503-when-unconfigured is the right gate.
    livekit_url: str = "wss://livekit.imagineering.cc"
    livekit_api_key: str = ""       # LiveKit API key id (goes in the token `iss`)
    livekit_api_secret: str = ""    # SECRET — host .env / SOPS, signs the token (HS256)
    # Join-token TTL. The token is validated only at CONNECT; the media session
    # outlives it, so a SHORT TTL is the point — a real join window is minutes, not
    # days. BOUNDED ABOVE (`le`), not just below: a token is a bearer capability, so
    # an env typo (`LIVEKIT_TOKEN_TTL_SECONDS=31536000`) must NOT silently mint
    # year-long join capabilities (cage-match #122 Carnot+Tesla+Wu HIGH — a config
    # dial that widened the trust boundary). Default 10 min (ample to click "join"),
    # hard ceiling 1h; both directions bounded, mirroring island_key_version. NOTE the
    # EFFECTIVE validity window is `ttl + livekit_tokens._NBF_LEEWAY_SECONDS` (a ~10s
    # nbf backdate for SFU clock skew), so the real ceiling is 3600+10s (cage-match
    # #122 rd2 Wu F3 — the bound stated here and the window the minter enforces differ
    # by exactly the leeway; naming it keeps that from drifting).
    livekit_token_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # --- APNs (push wake — #3267 increment 2) ---
    # The island wakes a CLOSED handset. Apple is the only party that can reach a
    # suspended iOS app, so APNs is a mandatory intermediary here in exactly the way
    # an SFU is mandatory for a browser behind a NAT — it buys REACH, nothing else.
    #
    # We talk to APNs DIRECTLY rather than through Firebase's bridge. That is the app
    # tab's recorded decision (`device_platform.dart`: "Google is not in the RUNTIME
    # PATH on Apple platforms") and this half must not silently contradict it — the
    # `Platform` enum's two values only mean something if each talks to its own
    # service. Android/FCM is a separate transport behind the same door, NOT built yet.
    #
    # OPTIONAL, exactly like LiveKit above: absent credentials mean the island runs
    # normally and simply never pushes. An operator standing up an island gets a
    # working island without an Apple developer account; notifications are opt-in.
    # This is why per-island credentials do not fight the one-script standup goal.
    apns_key_id: str = ""        # the 10-char Key ID of the .p8 (the JWT `kid`)
    apns_team_id: str = ""       # Apple Developer Team ID (the JWT `iss`)
    # The app's bundle id — APNs `apns-topic`. NOT derived from any other setting:
    # a device token is only valid for the topic it was issued under, so a wrong
    # topic is a silent 400 for every send, and it must be stated, not inferred.
    apns_topic: str = ""
    # SECRET — the .p8 signing key, PEM contents (host .env / SOPS), not a path.
    # Contents rather than a path deliberately: the container would otherwise need a
    # bind-mount whose absence fails at first-send (a runtime surprise) instead of at
    # boot, and the existing secret-delivery channel for this deployment is the .env.
    apns_private_key: str = ""
    # Sandbox vs production APNs host. A device token from a DEVELOPMENT build is
    # ONLY valid against api.sandbox.push.apple.com, and a TestFlight/App Store build's
    # token is ONLY valid against api.push.apple.com — the same token string against
    # the wrong host is a 400 BadDeviceToken with no other clue. There is no way to
    # tell the two apart by inspecting the token, so this is an explicit operator
    # switch and not something the code may guess.
    apns_use_sandbox: bool = False
    # Per-RECIPIENT wake budget. Waking a handset is a strictly louder capability than
    # delivering a message — a message you read when you choose, a push interrupts you
    # wherever you are — so it gets its own cap, keyed on the person being woken rather
    # than the sender's IP like the auth buckets. A DM peer who can legitimately send
    # can still only ring you N times a minute. Not an authn control; a blast-radius cap.
    apns_wake_per_recipient_per_minute: int = Field(default=6, ge=1, le=60)

    # Self-service registration. None → resolved by environment in the validator
    # (open in dev, closed in prod); set OPEN_REGISTRATION to override either way.
    open_registration: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_env_strings(cls, data):
        """Restore the one thing an environment variable cannot say: nothing.

        docker-compose's `environment:` mapping has no "omit if unset" — an unset
        host var interpolates to the empty string and the container var is still
        SET. So "the operator configured nothing" arrives as `""` (or, if their
        .env line has a stray space, as `"   "`), and pydantic is handed a value
        where it should have been handed absence.

        ONE RULE: a whitespace-only string means the operator said nothing, so the
        key is DELETED and pydantic's own default applies — whatever the field's
        type is. Earlier revisions of this validator asked "is the default None?"
        (cage-match PR#141 round 1) and then "is it a bool?" (round 2), and both are
        per-case allowlists inside a PR whose entire point is that allowlists cannot
        fail for the case nobody thought of. Tesla found the second one within a
        round: `RATE_LIMIT_ENABLED="   "` stripped to `""`, missed the None-default
        branch, and crash-looped — verified. Deleting the key covers bool, int, enum,
        tri-state and str in a single move, and needs no list to be kept current.

        Separately, a NON-STRING scalar is stripped, because whitespace can never be
        meaningful in a bool or an enum and `OPEN_REGISTRATION=false ` (one trailing
        space in a .env) otherwise raises ValidationError — verified, Tesla round 1.
        pydantic already tolerates padding on ints, but stripping is harmless there
        and keeps the rule uniform. String-typed fields are deliberately NOT
        stripped: APNS_PRIVATE_KEY is a PEM and GITHUB_CLIENT_SECRET is a
        credential, and their leading/trailing bytes are the caller's business.

        Fail-closed by construction: restoring absence for a REQUIRED field (say a
        whitespace-only JWT_SECRET) lets it fall to its dev default, which
        _harden_for_production then refuses to boot on — the loud failure, not the
        silent one.
        """
        if not isinstance(data, dict):
            return data
        for name, field in list(cls.model_fields.items()):
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, str):
                continue
            if value.strip() == "":
                del data[name]          # the environment said nothing; make it so
                continue
            if "str" not in str(field.annotation):
                data[name] = value.strip()
        return data
        for name, field in cls.model_fields.items():
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, str):
                continue
            annotation = str(field.annotation)
            if "bool" in annotation:
                value = value.strip()
            if value.strip() == "" and field.default is None:
                value = None
            data[name] = value
        return data

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
    # Provisioning token TTL: a brand-new social user gets a short-lived signed
    # token (NOT a DB row) to carry (provider, sub, suggested name/email) from the
    # verify step to the handle-claim step. Short window — it's a one-step
    # handoff, not a session.
    provisioning_ttl_seconds: int = 10 * 60  # 10 min
    # Minimum interval between HANDLE changes via PATCH /v1/me (#2631). Identity is
    # the KEY; the handle is a mutable label that @-mentions/DMs resolve against, so
    # rapid churn would destabilise resolution. 30 days (confirmed with Nick).
    # display_name edits are NOT rate-limited by this. Config so an island MAY tune
    # it (forwarded in docker-compose); the default is correct for every island.
    # ge=1: a trust-boundary throttle must not be silently disable-able by a
    # malformed env (HANDLE_CHANGE_COOLDOWN_SECONDS=-1/0 would make `elapsed <
    # cooldown` never true → no cooldown). Bounded like island_key_version; a boot
    # with a non-positive value fails LOUD rather than fail-open (cage-match #118).
    handle_change_cooldown_seconds: int = Field(default=30 * 24 * 3600, ge=1)  # 30 days

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

    # --- island identity + moderation mode (crucible-09 Phase A) ---
    # The island's elected moderation posture, signed into its self-manifest
    # (GET /v1/island) so it's HONEST and LEGIBLE to clients before a user speaks.
    #   moderator = the shipped status quo made explicit: the gateway holds plaintext,
    #              the report queue + #7 takedown/retraction machinery operate, and the
    #              operator carries the scan/report duties (design note 07). Default,
    #              because it matches both live islands' reality.
    #   e2ee      = SCHEMA-RESERVED for Phase B (MLS client-side encryption). No
    #              encryption exists yet, so advertising it would be the exact mislabel
    #              this feature prevents (users believing E2EE while the operator reads
    #              plaintext). It is HARD-REJECTED at boot in EVERY environment until
    #              Phase B lands (see _harden_for_production) — the value is in the enum
    #              only so the wire/type vocabulary is forward-stable, never selectable.
    island_mode: IslandMode = IslandMode.MODERATOR
    # The island's long-lived Ed25519 identity key, as an unpadded-base64url 32-byte
    # seed. Signs the self-manifest (island_identity.py). SECRET — supplied via the
    # host .env (SOPS in deploy), NEVER committed. Dev default is _DEV_ISLAND_SEED; a
    # production boot on that default is rejected (same fail-closed posture as
    # jwt_secret). The private key never leaves the process; only the public Multikey
    # + signatures are exposed.
    island_signing_seed: str = _DEV_ISLAND_SEED
    # The signing key's version, carried in the manifest for a future rotation
    # lifecycle (#1865). Bumped when the seed is rotated so a verifier can tell keys
    # apart. 1 until the first rotation. Bounded to a u32 (ge=1): the manifest packs
    # it as a big-endian u32 in the signing bytes, so an out-of-range value must fail
    # CLOSED at boot (a clear ValidationError) rather than raising struct.error as a
    # 500 on the first GET /v1/island.
    island_key_version: int = Field(default=1, ge=1, le=2**32 - 1)
    # A5 (crucible-09): electing `moderator` mode is a COMMITMENT, not just a label.
    # In PRODUCTION, booting in moderator mode requires the operator to acknowledge the
    # CSAM/illegal-content runbook (docs/design/07 Part B) by setting this True. This
    # enforces the HONEST, bounded guarantee the crucible settled on (DESIGN.md §5):
    # "machinery present + acknowledged", NOT "the operator actually reviews reports" —
    # code cannot force a human to act, and this flag does not pretend to. Default False
    # so a fresh prod island in the default (moderator) mode fails closed at boot until
    # the operator consciously acknowledges; dev/test islands are exempt (see
    # _harden_for_production — the check is prod-only because moderator is the DEFAULT
    # mode and an every-env gate would break every dev boot). Env: CSAM_RUNBOOK_ACKNOWLEDGED.
    csam_runbook_acknowledged: bool = False
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
        # Normalize the moderator seat list at the Settings boundary (EVERY env): strip
        # each id and drop empties, so the STORED value equals what require_moderator /
        # is_moderator matches against at runtime (exact string membership,
        # domain/moderation_service.py). Without this the A5 presence gate below and the
        # runtime dep check would run on DIFFERENT strings — a PADDED id ("  real-ulid  ")
        # would satisfy a strip-for-truthiness presence check yet never match a clean JWT
        # user_id, 403-ing every real moderator: the empty-set disable-vector in costume
        # (PR#113 cage-match, Tesla HIGH). Normalize ONCE here so the gate and the dep
        # share one canonical string; a whitespace-only entry collapses to nothing and is
        # then indistinguishable from an empty set (correctly refused by A5 in prod).
        self.moderator_user_ids = [u.strip() for u in self.moderator_user_ids if u.strip()]
        # Normalize the LiveKit creds at the Settings boundary (EVERY env): a
        # whitespace-only key/secret ("  ") would otherwise read as "configured" in
        # is_configured() and mint tokens the SFU rejects instead of the honest 503
        # (cage-match #122 Carnot). Strip once here so the STORED value equals what
        # is_configured() tests AND what signs the token — a padded secret can't
        # silently sign a different byte string than the operator set.
        self.livekit_api_key = self.livekit_api_key.strip()
        self.livekit_api_secret = self.livekit_api_secret.strip()
        # Strip gateway_id too (cage-match #122 rd3 Tesla+Wu): it namespaces LiveKit
        # rooms/identities, so a padded "  island-a  " would mint under a non-canonical
        # prefix AND slip past the `if not self.gateway_id` prod gate below. Normalize
        # once here so the stored value is what actually prefixes the room string — the
        # same asymmetric-strip fix already applied to moderator_user_ids and the creds.
        self.gateway_id = self.gateway_id.strip()
        # LiveKit is OPTIONAL, but WHEN configured it mints bearer capabilities to a
        # SHARED SFU (one API key across islands). The forgery + cross-island-collision
        # risk is a function of pointing at a REMOTE/shared SFU, NOT of ENVIRONMENT
        # (cage-match #122 rd5 Wu F1 — gating on is_production let a non-prod box with
        # the real shared creds + no gateway_id merge its users into prod's rooms). So
        # these guards fire whenever LiveKit is configured against a non-loopback SFU,
        # in EVERY environment; a loopback dev SFU (ws://localhost) is exempt.
        if self.livekit_api_key or self.livekit_api_secret:
            if not (self.livekit_api_key and self.livekit_api_secret):
                raise ValueError(
                    "LiveKit is half-configured: set BOTH livekit_api_key and "
                    "livekit_api_secret, or NEITHER. Refusing to boot."
                )
            host = urlparse(self.livekit_url).hostname or ""
            if not host:  # e.g. "wss://" — a prefix check would pass it (Wu F3)
                raise ValueError(
                    f"livekit_url has no host (got {self.livekit_url!r}). Refusing to boot."
                )
            # Exempt a loopback SFU ONLY in a non-prod env (dev convenience). A loopback
            # URL in PRODUCTION is either misconfigured (mobile clients resolve
            # `localhost` to the DEVICE, not the island) or a hardening bypass — so a
            # prod island gets the full guards regardless of host (cage-match #122 rd6
            # Carnot: the rd5 loopback exemption must not reach prod).
            is_loopback = host in {"localhost", "127.0.0.1", "::1"}
            if not (is_loopback and not self.is_production):
                if urlparse(self.livekit_url).scheme != "wss":
                    raise ValueError(
                        f"livekit_url must be wss:// for a remote SFU (got {self.livekit_url!r}) "
                        "— it is handed to every client; a plaintext/wrong-scheme URL is a "
                        "client-redirect footgun. Refusing to boot."
                    )
                if len(self.livekit_api_secret) < _MIN_PROD_SECRET_LEN:
                    raise ValueError(
                        f"livekit_api_secret is too weak (len={len(self.livekit_api_secret)} "
                        f"< {_MIN_PROD_SECRET_LEN}) for a remote SFU. A weak-but-non-empty "
                        "secret makes every minted room token forgeable. Refusing to boot."
                    )
                if not self.gateway_id:
                    raise ValueError(
                        "gateway_id is required when LiveKit is configured against a "
                        "REMOTE/shared SFU (in ANY environment): the SFU is shared across "
                        "islands on ONE API key, so rooms/identities MUST be namespaced by "
                        "gateway_id or they collide across islands (the empty default is the "
                        "fail-open case). Refusing to boot — set GATEWAY_ID."
                    )
        # APNs, like LiveKit, is OPTIONAL — but HALF-configured is the dangerous
        # state, not the absent one. Absent credentials are honest: is_configured()
        # is False, nothing is sent, and the operator knows push is off. A partial
        # set reads as "push is on" at every call site while every send fails at
        # Apple's door, and the failure is INVISIBLE because a push has no user-
        # visible success either — a missed call and a disabled feature look
        # identical on the handset. So require all four together or none, at boot,
        # where an operator is watching, rather than at first ring.
        _apns = {
            "apns_key_id": self.apns_key_id.strip(),
            "apns_team_id": self.apns_team_id.strip(),
            "apns_topic": self.apns_topic.strip(),
            # NOT stripped: a PEM's trailing newline is part of the file and
            # cryptography accepts either, but leading whitespace breaks the
            # "-----BEGIN" header match. lstrip only.
            "apns_private_key": self.apns_private_key.lstrip(),
        }
        for name, value in _apns.items():
            setattr(self, name, value)
        if any(_apns.values()) and not all(_apns.values()):
            missing = sorted(k for k, v in _apns.items() if not v)
            raise ValueError(
                f"APNs is half-configured — missing {missing}. Set ALL of "
                f"{sorted(_apns)} or NONE. A partial set silently fails every "
                "push at Apple's door, which is indistinguishable on the handset "
                "from push being switched off. Refusing to boot."
            )
        if all(_apns.values()):
            # PRESENCE IS NOT PARSEABILITY (cage-match #139, Maxwell). The guard
            # above earns its keep by failing "where an operator is watching,
            # rather than at first ring" — but truthiness alone lets the single
            # most common dotenv mistake straight through: a PEM pasted with
            # literal backslash-n instead of real newlines. That boots clean,
            # then `jwt.encode` raises on the first ring, gets swallowed by
            # push_service's broad except, and logs "wake failed" forever. The
            # invisible failure the guard exists to prevent, one layer deeper.
            #
            # So actually LOAD the key, and require it to be EC: ES256 is an
            # elliptic-curve algorithm, and an RSA .p8 (or an App Store Connect
            # key pasted by mistake — they look identical on disk) satisfies
            # every presence check and cannot sign a single push.
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key,
            )
            try:
                _key = load_pem_private_key(
                    self.apns_private_key.encode(), password=None)
            except Exception as ex:
                raise ValueError(
                    "apns_private_key is not a readable PEM private key "
                    f"({type(ex).__name__}). The usual cause is a .env that "
                    r"contains the literal two characters \n instead of real "
                    "newlines — check the key survived transport intact. "
                    "Refusing to boot rather than failing invisibly at the "
                    "first ring."
                ) from ex
            if not isinstance(_key, ec.EllipticCurvePrivateKey):
                raise ValueError(
                    "apns_private_key parses but is not an elliptic-curve key "
                    f"(got {type(_key).__name__}). APNs provider tokens are "
                    "ES256, so only an EC key can sign them — this is most "
                    "likely the wrong .p8 (an App Store Connect API key looks "
                    "identical on disk). Refusing to boot."
                )
            if not isinstance(_key.curve, ec.SECP256R1):
                # PARSEABLE IS NOT USABLE (cage-match #139 round 2, Carnot) — the
                # same ladder as presence-is-not-parseability one rung further
                # down. ES256 is not "EC"; it is P-256 SPECIFICALLY. A P-384 or
                # P-521 key is a real EC key, satisfies the isinstance check
                # above, and still cannot sign a provider token — pushing the
                # failure right back into the swallowed background send path this
                # whole validator exists to keep it out of.
                raise ValueError(
                    "apns_private_key is an EC key on the wrong curve "
                    f"({_key.curve.name}). ES256 requires P-256 (secp256r1) "
                    "specifically, so this key parses but can never sign an APNs "
                    "provider token. Refusing to boot."
                )

        # A2 (crucible-09 Phase A): `e2ee` is schema-reserved for Phase B and
        # HARD-REJECTED in EVERY environment until MLS lands. Advertising an
        # unimplemented E2EE mode would be the exact mislabel this feature prevents
        # (users believe E2EE while the gateway still holds plaintext). NOT
        # prod-gated: a dev/test island advertising e2ee would lie to its client
        # just the same (A3 reads the manifest in dev too). The value stays in the
        # enum so the wire vocabulary is forward-stable — it is simply never bootable
        # in Phase A. Phase B lifts this guard when real client-side encryption ships.
        if self.island_mode == IslandMode.E2EE:
            raise ValueError(
                "island_mode='e2ee' is not available in Phase A: no client-side "
                "encryption is implemented yet, so advertising E2EE would mislead "
                "users into believing their messages are unreadable by the operator "
                "when the gateway still holds plaintext. The value is reserved for "
                "Phase B (MLS). Refusing to boot — set ISLAND_MODE=moderator."
            )
        # The island identity seed must decode to a 32-byte Ed25519 seed in EVERY
        # environment — a malformed key cannot sign the self-manifest, so fail closed
        # at boot rather than 500 on the first GET /v1/island. Lazy import: this
        # pulls in domain.island_identity -> domain.signing, and config is imported
        # very early (peers_service imports it at module load); a top-level import
        # would risk a config<->domain cycle.
        from .domain.island_identity import IslandIdentityError, decode_seed
        try:
            decode_seed(self.island_signing_seed)
        except IslandIdentityError as e:
            raise ValueError(f"ISLAND_SIGNING_SEED is invalid: {e}") from e

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
            # The island identity key is a trust root (it signs the self-manifest);
            # a prod boot on the dev default would let anyone who read this repo sign
            # a manifest for this island. Same fail-closed posture as jwt_secret.
            # Compare the decoded KEY BYTES, not the spelling: the guard's intent is
            # "don't run on the dev KEY", and a byte compare is robust to any base64url
            # aliasing independently of the (now canonical) decoder — the seed already
            # decoded cleanly at the top of this validator, so decode_seed is safe here.
            from .domain.island_identity import decode_seed as _decode_seed
            if _decode_seed(self.island_signing_seed) == _decode_seed(_DEV_ISLAND_SEED):
                raise ValueError(
                    "island_signing_seed is still the dev default in a production "
                    f"environment (environment={self.environment!r}). Refusing to "
                    "boot — supply a real ISLAND_SIGNING_SEED (32 random bytes as "
                    "unpadded base64url, e.g. "
                    "`python -c \"import os,base64; "
                    "print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode())\"`, "
                    "via SOPS)."
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
            # A5 (crucible-09): "moderator = commitment" — the capstone prod gate.
            # Electing `moderator` mode in production makes two capability guarantees
            # structurally true at boot, so the dangerous "island advertises moderator
            # while moderation is effectively off" config is UNBOOTABLE (not merely
            # discouraged). Prod-only: moderator is the DEFAULT mode, so an every-env gate
            # would refuse every dev/test boot — dev islands run moderator freely (no real
            # election / legal exposure); the LIVE islands are is_production and carry the
            # teeth (BLADE.md: "forbidden on the live islands"). HONEST LIMIT (DESIGN.md
            # §5): this forces the machinery PRESENT + ACKNOWLEDGED; it CANNOT force a
            # human to actually review reports, and deliberately does not claim to. Placed
            # LAST among the prod gates so a config that also trips an earlier invariant
            # (weak secret, no ingress, bad rp_id) surfaces THAT cause first.
            if self.island_mode == IslandMode.MODERATOR:
                # (1) A moderator must be CONFIGURED. An empty moderator set is the
                # concrete "plaintext with moderation deleted" state: the report queue
                # still accepts reports, but require_moderator (rest/deps.py) 403s EVERYONE,
                # so nobody can take anything down. The list was stripped+compacted at the
                # top of this validator, so a whitespace-only or padded-to-empty entry has
                # already collapsed to nothing — a plain emptiness check is exact here AND
                # matches the runtime dep's string (no asymmetric strip; PR#113 Tesla HIGH).
                # SCOPE / KNOWN RESIDUAL (PR#113 cage-match, Tesla): this is a PRESENCE
                # check on the id STRING, not a LIVENESS check on the account. A boot-time
                # Settings validator has no DB, so it cannot confirm the id belongs to a
                # real, registered, authenticatable user — a "ghost" id
                # (MODERATOR_USER_IDS=["never-registered-ulid"]) satisfies this yet can
                # never pass require_moderator, re-tuning the disable-vector rather than
                # eliminating it. So the honest guarantee is "an EMPTY moderator set cannot
                # boot moderator mode", NOT "a live moderator is guaranteed present."
                # Seat LIVENESS/health is a runtime/ops invariant (a startup or periodic
                # check that at least one configured id maps to a real user), tracked
                # separately — this closes the empty-set vector, not the ghost-seat one.
                if not self.moderator_user_ids:
                    raise ValueError(
                        "island_mode='moderator' in production requires at least one "
                        "configured moderator (MODERATOR_USER_IDS): a moderator island "
                        "with no moderator has a write-only report queue that "
                        "require_moderator 403s everyone from acting on — the exact "
                        "'plaintext with moderation deleted' config this mode forbids. "
                        "Appoint a moderator, or change ISLAND_MODE."
                    )
                # (2) The operator must acknowledge the CSAM/illegal-content runbook.
                # Electing moderator mode commits the operator to the scan/report/takedown
                # duties (design note 07 Part B). Enforces acknowledgement at election —
                # NOT that reports are reviewed (code can't force that; stated honestly).
                if not self.csam_runbook_acknowledged:
                    raise ValueError(
                        "island_mode='moderator' in production requires "
                        "CSAM_RUNBOOK_ACKNOWLEDGED=true — electing moderator mode is a "
                        "commitment to the operator CSAM/illegal-content runbook "
                        "(docs/design/07 Part B). This enforces machinery-present + "
                        "acknowledged, NOT that reports are actually reviewed. "
                        "Acknowledge the runbook, or change ISLAND_MODE."
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
