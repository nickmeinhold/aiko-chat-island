"""ORM models — Phase 1 subset (plan §A4).

Hand-written SQLAlchemy 2.0 ORM (no codegen). This is the persistence half of
the trust boundary: `messages.sender_user_id` is set server-side from the
authenticated user (invariant I5), never from client input. Reactions, media,
read_positions, devices, message_edits arrive in later phases (each its own
alembic revision).
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .ids import new_ulid


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Role(enum.StrEnum):
    """Closed set of membership roles. StrEnum (3.12) so the value IS the string
    stored in the column. Defined here (the persistence layer) so it is the single
    source of truth for BOTH the column default and the DB CHECK constraint below;
    re-exported from memberships_service for the call sites (cage-match PR#10/#23)."""

    ADMIN = "admin"
    MEMBER = "member"


class JoinPolicy(enum.StrEnum):
    """Closed set of private-channel self-join policies (see Channel.join_policy).
    'invite_only' = admin-add only; 'open' = any authed user may self-join."""

    INVITE_ONLY = "invite_only"
    OPEN = "open"


class Platform(enum.StrEnum):
    """Closed set of push-notification platforms (#16). 'apns' = Apple Push
    Notification service; 'fcm' = Firebase Cloud Messaging (Android). Same
    single-source-of-truth pattern as Role/JoinPolicy: the enum drives the DB
    CHECK on device_tokens.platform via _in_check, so the constraint can't drift
    from the Python closed set."""

    APNS = "apns"
    FCM = "fcm"


class PasskeyOperation(enum.StrEnum):
    """Closed set of WebAuthn ceremony types (#1471). Same single-source-of-truth
    pattern as Role/JoinPolicy/Platform: drives the DB CHECK on
    passkey_challenges.operation via _in_check, so the constraint can't drift from
    the Python closed set. Pinning the operation stops a register challenge from
    completing an authenticate ceremony (or vice versa)."""

    REGISTER = "register"
    AUTHENTICATE = "authenticate"


def _in_check(column: str, values: type[enum.StrEnum]) -> str:
    """SQL `column IN ('a', 'b')` derived FROM the enum members, so the DB CHECK
    can never drift from the Python closed set — change the enum, the constraint
    follows (#11)."""
    rendered = ", ".join(f"'{m.value}'" for m in values)
    return f"{column} IN ({rendered})"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # NULLABLE as of social sign-in (#13): a social-only account has no password.
    # The authenticate() path MUST guard `password_hash is None` BEFORE argon2 so
    # a null hash can never become a password-auth shortcut (the social bypass).
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Wire-attribution identity on the aiko bus (defaults to username).
    aiko_username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Informational ONLY (#13). Identity authority is the (provider, sub) pair in
    # social_identities, NEVER email — with no email-verification step an
    # email-match would be account takeover. Nullable: Apple only returns email on
    # first consent (and may be a private-relay address).
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SocialIdentity(Base):
    """A verified federated identity → local user link (#13).

    The (provider, provider_sub) pair is the SOLE identity authority for social
    sign-in. UNIQUE on that pair so a provider subject maps to exactly one local
    user. Multi-provider linking (one user, several identities) is DEFERRED and
    must require re-auth — never email equality.
    """
    __tablename__ = "social_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_sub", name="uq_social_provider_sub"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # apple|google
    provider_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Channel(Base):
    __tablename__ = "channels"
    # DB-level closed-set enforcement beyond the API boundary (#11): even a direct
    # SQL write (a migration, a repl, a future bug bypassing the service clamp)
    # cannot store an out-of-set join_policy.
    __table_args__ = (
        CheckConstraint(_in_check("join_policy", JoinPolicy),
                        name="ck_channels_join_policy"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 'standard' | 'llm' | 'robot' | 'dm' — maps to an aiko recipient string.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    aiko_channel: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Self-join policy for PRIVATE channels (#46). 'invite_only' (default) = a
    # user may only be added by a channel admin; 'open' = any authenticated user
    # may self-join via /join. Public channels (is_private=False) ignore this —
    # they are open to everyone and need no membership at all. Default is the
    # safe one: a private channel is invite_only until explicitly opened, so a
    # channel created without thinking about policy can never be self-joined.
    # The closed set is the JoinPolicy StrEnum (memberships_service); stored as
    # its string value so the column type is a plain VARCHAR.
    join_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="invite_only")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    # DB-level role closed-set enforcement beyond the API boundary (#11).
    __table_args__ = (
        CheckConstraint(_in_check("role", Role), name="ck_memberships_role"),
    )
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # member|admin
    can_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Idempotent optimistic send: a resent client_msg_id no-ops, not dupes.
        UniqueConstraint("channel_id", "client_msg_id", name="uq_channel_client_msg"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), nullable=False, index=True)
    # Null when the sender is a non-gateway aiko actor (llm/robot/external REPL).
    sender_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sender_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # human|llm|robot|actor
    sender_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    client_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True if it arrived FROM the bus; False if it originated gateway-side.
    aiko_origin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserBlock(Base):
    """A user-to-user block (UGC moderation, Apple 1.2 / Google UGC, #7).

    DIRECTIONAL storage, MUTUAL effect. The row records who initiated
    (``blocker_user_id``) so the blocker can later unblock exactly the people
    they blocked, but the *visibility* it produces is symmetric: neither party
    sees the other's messages once a row exists in either direction (see
    ``moderation_service.blocked_pair_user_ids`` / the history+fence predicate).
    Composite PK makes a re-block idempotent (one row per ordered pair), mirroring
    ``Membership``. No ``ON DELETE CASCADE``: account deletion tears these down
    explicitly in ``accounts_service`` (children-before-parent), like every other
    child of ``users``.
    """
    __tablename__ = "user_blocks"
    blocker_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True)
    blocked_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MessageReport(Base):
    """A user's report of an objectionable message (UGC moderation, #7).

    Write-mostly: reports never affect any read path — they feed the ops queue
    that backs the EULA's "act within 24h" commitment. Acting on a report reuses
    the EXISTING soft-delete (``Message.deleted_at``) — there is no separate
    takedown table. UNIQUE(message_id, reporter_user_id) makes a double-report a
    no-op (one standing report per reporter per message). ``reporter_user_id`` is
    NULLABLE so account deletion can ANONYMIZE a reporter (mirroring how authored
    messages anonymize) and keep the report for ops rather than destroying the
    audit trail. ``resolved_at`` is stamped when ops actions the report.
    """
    __tablename__ = "message_reports"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "reporter_user_id", name="uq_report_message_reporter"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id"), nullable=False, index=True)
    reporter_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class DeviceToken(Base):
    """A user's registered push-notification device token (#16, increment 1).

    The token is issued by APNs/FCM per app INSTALL and routes a push to exactly
    one physical device. It is GLOBALLY UNIQUE, not unique-per-user: the same
    device (same token) can change hands between accounts (logout A → login B on
    the same phone), and a push must always reach the CURRENT owner. So
    registration is an UPSERT KEYED ON THE TOKEN that reassigns ``user_id`` —
    never a second row (see ``devices_service.register_device``). A stale row for
    a previous owner would misroute that device's notifications; UNIQUE(token)
    makes a duplicate impossible.

    No ON DELETE CASCADE: this codebase never relies on it (cf. accounts_service /
    channels_service.hard_delete_channel). Account deletion tears these down
    explicitly, children-before-parent.

    SECURITY NOTE for increment 2 (actual sending): reassign-on-conflict means an
    actor who somehow obtains another device's token could redirect that device's
    push routing (a DoS / misdirected-spam vector — NOT a data leak, since pushes
    are looked up by the recipient's user_id). The token is a device-held secret
    not exposed by any read path; treat token confidentiality as the boundary and
    revisit at the send-path cage-match.
    """
    __tablename__ = "device_tokens"
    # Named constraints (not column-level unique=True) so the ORM metadata matches
    # the hand-written 0003 migration EXACTLY — the parity gate (test_migrations
    # .test_migrations_match_models) diffs reflected unique constraints, and an
    # unnamed column-unique would not match the named one in the migration.
    __table_args__ = (
        UniqueConstraint("token", name="uq_device_tokens_token"),
        CheckConstraint(_in_check("platform", Platform),
                        name="ck_device_tokens_platform"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(8), nullable=False)
    # APNs tokens are 64 hex chars; FCM registration tokens are ~160+ and grow —
    # 512 is comfortable headroom. UNIQUE (named, above): one row per device token.
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class OAuthHandoff(Base):
    """A one-time handoff for the server-side OAuth broker flow (#21).

    The broker completes the authorization-code exchange SERVER-side and must
    return the result to the app WITHOUT putting minted tokens in a redirect URL
    (a redirect URL leaks into browser history / referrer / server logs). So the
    callback stores a MINIMAL outcome payload here under a fresh random code and
    redirects the browser to the app with only that opaque ``?code=``; the app
    then POSTs ``/v1/auth/oauth/exchange`` to redeem it for the real tokens.

    SECURITY shape:
      * ``code`` is the PK and is ``secrets.token_urlsafe(32)`` — cryptographically
        random, unguessable, single-use.
      * ``payload`` stores ONLY the minimal outcome (a user_id for a known user, or
        the verified-identity fields for provisioning) — NEVER minted access/refresh
        tokens. Tokens are minted at redemption time, so a stolen-but-unredeemed
        row yields no usable credential and an expired/consumed one yields nothing.
      * ``consumed`` + ``expires_at`` enforce single-use within a short TTL. The
        redemption marks consumed ATOMICALLY (a guarded UPDATE) to close the
        double-spend race.

    No ON DELETE anything — rows are short-lived (≈2 min TTL) and self-expire; a
    sweeper is unnecessary at this scale (a follow-up if the table ever grows).
    """
    __tablename__ = "oauth_handoffs"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class OAuthState(Base):
    """A one-time CSRF/PKCE state nonce for the server-side OAuth broker (#21).

    Replaces the earlier self-contained signed-JWT ``state`` (cage-match #30,
    Finding 1). Two reasons the JWT had to go:

      * PKCE LEAK — the JWT carried the PKCE ``code_verifier`` through the browser
        and the provider (it is base64-readable in the URL), which defeats the
        whole point of PKCE for any future PKCE-enabled provider. Now the verifier
        is stored SERVER-SIDE in this row and ONLY the ``code_challenge`` ever
        leaves us — the verifier never crosses the wire.
      * REPLAY / login-CSRF — a signed-but-stateless state is replayable within
        its exp window and is not bound to a single use. This row makes ``state``
        an opaque, single-use nonce: ``consumed`` + ``expires_at`` mean a captured
        callback URL cannot be replayed at the state layer (the prior design
        leaned on the provider code's single-use property as the only backstop —
        that NAMED TRADEOFF is now RETIRED).

    SECURITY shape (mirrors OAuthHandoff):
      * ``nonce`` is the PK and is ``secrets.token_urlsafe(32)`` — 256 bits,
        unguessable, single-use.
      * ``code_verifier`` is nullable (only PKCE providers store one) and NEVER
        leaves the server.
      * ``consumed`` + ``expires_at`` enforce single-use within a short TTL; the
        callback marks consumed ATOMICALLY (a guarded UPDATE) to close the
        double-spend / replay race.

    Rows are short-lived (the oauth_state TTL) and self-expire; no sweeper at this
    scale (a follow-up if the table ever grows).
    """
    __tablename__ = "oauth_states"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The APP's S256 challenge (cage-match #37): base64url(sha256(app_verifier)),
    # supplied by the app at /start and carried into the handoff so /exchange can
    # require the matching verifier. Binds the handoff to the originating app, so a
    # custom-scheme-intercepted handoff code is unredeemable. Distinct from
    # code_verifier (the gateway↔provider PKCE secret).
    app_challenge: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class SocialNonce(Base):
    """A one-time, server-ISSUED nonce for the NATIVE social sign-in flow (#13,
    option (a)). Distinct from OAuthState: that is broker (server-side code-flow)
    state carrying PKCE/provider routing; this is a bare single-use token whose
    only job is to make the sign-in nonce INDEPENDENT SERVER STATE.

    Why this closes the replay window PR#32 (option (b)) left open: there the APP
    generated the nonce and sent it beside the id_token, so the 'expected' value
    was attacker-replayable (capture the body, replay both — and for Google the raw
    nonce is even readable out of the token). Here the GATEWAY issues the nonce,
    stores it, and CONSUMES it exactly once at /social. A captured request can't be
    replayed because the nonce is already burned — the defense no longer depends on
    the attacker never seeing the nonce.

    Shape mirrors OAuthState's single-use guarantee (consumed + expires_at, atomic
    guarded UPDATE) but carries NO provider/verifier — a native nonce is
    provider-agnostic at issue time (the app picks Apple or Google afterwards).
    Rows are short-lived (the social_nonce TTL) and self-expire; no sweeper at this
    scale (a follow-up if the table ever grows).
    """
    __tablename__ = "social_nonces"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class PasskeyCredential(Base):
    """A registered WebAuthn passkey (#1471). A passkey is a CREDENTIAL — an
    authenticator-held keypair — not a federated identity, so it gets its own table
    rather than a SocialIdentity row. authenticate/finish looks a credential up by
    credential_id, verifies the assertion against public_key, enforces a strictly
    increasing sign_count (authenticator-clone detection), then issues a session
    for user_id.

    Why this is the security win of the passkey pivot: a leaked DB yields only
    PUBLIC keys, which are worthless — the private key never leaves the
    authenticator. There is no shared secret to steal (contrast password_hash).
    """
    __tablename__ = "passkey_credentials"
    # credential_id is the authenticator's globally-unique handle (base64url) and is
    # the lookup key in authenticate/finish, hence UNIQUE. We keep a ULID surrogate
    # PK to match the table convention and avoid a long-string primary-key index.
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    credential_id: Mapped[str] = mapped_column(
        String(512), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)  # base64url(COSE)
    # uint32 per the WebAuthn spec; BigInteger leaves headroom and never overflows
    # the monotonic clone-detection comparison.
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transports: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    aaguid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class PasskeyChallenge(Base):
    """A single-use WebAuthn ceremony challenge (#1471). Mirrors OAuthState's
    single-use guarantee (consumed + expires_at, atomic guarded UPDATE — see
    passkey_service.consume_challenge). `state` is the opaque handle the app
    round-trips start -> finish; its decoded bytes ARE the WebAuthn challenge the
    authenticator signs over, so the row is both the DB key and the
    expected_challenge (no separate column). `operation` pins a challenge to the
    ceremony that minted it. No user_id: register is anonymous
    (first-passkey-creates-account) and authenticate is usernameless/discoverable,
    so neither flow knows the user at start.
    """
    __tablename__ = "passkey_challenges"
    __table_args__ = (
        CheckConstraint(
            _in_check("operation", PasskeyOperation),
            name="ck_passkey_challenges_operation"),
    )
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
