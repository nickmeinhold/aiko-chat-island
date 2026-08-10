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
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    PrimaryKeyConstraint, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .ids import new_ulid


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# The fixed PK of the single default community ("Aiko") seeded by migration 0009
# (#32, Phase B1). NOT a generated ULID: an all-zero id is an unmistakable
# sentinel for the system community, and a known constant lets `upsert_channel`
# assign it WITHOUT a "find-or-create the default" lookup — the contested "which
# community?" state is never entered, so there is no race window to guard (the
# remove-the-coupling move). Channels born from the bus inherit it via the
# `Channel.community_id` model default below.
DEFAULT_COMMUNITY_ID = "0" * 26


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


class ChannelKind(enum.StrEnum):
    """Closed set of channel kinds (#2633). SECURITY-RELEVANT since DMs: the WS send
    path gates bus federation on ``kind != DM`` (a DM never crosses the shared
    ChatServer — design 11 §Decision 3), so a channel whose kind is not a trusted
    member of this set could silently bypass that routing. Same single-source pattern
    as Role/JoinPolicy/Visibility: drives the DB CHECK on channels.kind via _in_check,
    so the constraint can't drift from the Python closed set and a direct SQL / buggy
    writer cannot store an out-of-set kind (cage-match PR#124 Carnot+Tesla: the privacy
    gate must not rest on an unenforced open string).

    Members: 'standard' = an ordinary bus-reconciled channel (the only kind any writer
    produces today — verified against live prod: all channels are 'standard'); 'llm' /
    'robot' = aiko actor channels (mapped to sender_kind by messages_service._kind_for);
    'dm' = a 1:1 direct-message channel (island-local, never federated).

    'group' is deliberately NOT pre-permitted (cage-match PR#124 Tesla): the member-set
    model keeps groups additive, but a group is ALSO community-less, and
    ck_channels_community_required only exempts 'dm'. Pre-permitting the kind without the
    matching community rule would let a future group ship into Aiko by default (the exact
    footgun this PR closes for DMs). So the group PR adds 'group' to THIS enum AND its
    community-exemption in ONE migration — a small, correct step, not a pre-committed
    half-invariant."""

    STANDARD = "standard"
    LLM = "llm"
    ROBOT = "robot"
    DM = "dm"


class Visibility(enum.StrEnum):
    """Closed set of community visibility levels (#32). 'public' = listed in the
    discovery directory and joinable by anyone; 'unlisted' = joinable via a direct
    link but never listed; 'private' = invite-only and never listed. Same
    single-source pattern as Role/JoinPolicy: drives the DB CHECK on
    communities.visibility via _in_check, so the constraint can't drift from the
    Python closed set. The discovery endpoint (B2) lists ONLY 'public'."""

    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"


class Category(enum.StrEnum):
    """Closed set of community categories for the discovery directory (#32). A
    small curated set so the directory can offer category filters; drives the DB
    CHECK on communities.category via _in_check. Expandable in a later migration
    as the directory grows (a new member here needs a matching revision that
    rebuilds the CHECK — the parity gate enforces that)."""

    GENERAL = "general"
    TECH = "tech"
    GAMING = "gaming"
    SOCIAL = "social"
    MUSIC = "music"
    EDUCATION = "education"
    OTHER = "other"


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
    # Social recovery (Design 05): the single-use server nonce a recover/start
    # issues is a PasskeyChallenge row pinned to this operation, so a register /
    # authenticate challenge can never complete a recovery (and vice versa) — the
    # same ceremony-pinning guard the other two members provide. Adding a member
    # here rebuilds the passkey_challenges CHECK via _in_check; the matching
    # migration (0013) must rebuild the DB CHECK to match, or the parity gate fails.
    RECOVER = "recover"


class ReportResolution(enum.StrEnum):
    """Closed set of moderator outcomes for a message report (Piece B, #7). Same
    single-source-of-truth pattern as Role/Platform: drives the DB CHECK on
    message_reports.resolution via _in_check, so the constraint can't drift from
    the Python closed set. NULL resolution = the report is still pending (not yet
    actioned); a resolved report carries one of these. 'taken_down' = the message
    was soft-deleted; 'dismissed' = the report was judged frivolous, message kept."""

    TAKEN_DOWN = "taken_down"
    DISMISSED = "dismissed"


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
    # Moderation ban (Piece B, #7). NULL = active; a timestamp = suspended FROM
    # this island (per-island, reversible, forward-looking — see moderation_service.
    # ban_user). Enforced at every auth ingress via users_service.is_banned; it does
    # NOT delete the user row (distinct from account deletion) nor their past
    # messages (use take-down for content).
    banned_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Session-revocation generation (#1914). Every access/refresh token embeds the
    # `gen` it was minted at; a token is honoured only while its gen == this column.
    # Bumping it (recovery finalize re-key) invalidates EVERY outstanding token for
    # the user in one write — the revocation mechanism stateless HS256 JWTs lack.
    # Checked live at every auth ingress (get_current_user, refresh, WS handshake),
    # exactly like is_banned. DEFAULT 0 = the un-revoked baseline; pre-#1914 tokens
    # (no `gen` claim) read as 0 too, so a deploy doesn't mass-logout live sessions.
    token_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    # Timestamp of the last handle (username) CHANGE (#2631). NULL = never changed
    # since creation, so the first change is always allowed. Stamped server-side by
    # the PATCH /v1/me mutate path ONLY when the handle actually changes; the mutate
    # path refuses another change while (now - handle_changed_at) is within
    # settings.handle_change_cooldown_seconds (429). display_name edits and a no-op
    # same-handle write never touch it. Identity is the KEY (id); this only rate-
    # limits churn on the mutable label so @-mentions/DMs that resolve key->handle
    # stay stable (see project_identity_social_cluster).
    handle_changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


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
        # Closed set for the channel kind (#2633, cage-match PR#124). SECURITY-relevant:
        # the WS send path gates bus federation on kind != 'dm', so an out-of-set kind
        # must be unrepresentable at the DB, not merely by convention (migration 0020).
        CheckConstraint(_in_check("kind", ChannelKind), name="ck_channels_kind"),
        # Community membership is now BIDIRECTIONAL by kind (#2633, cage-match PR#124
        # Carnot): a NON-DM channel MUST have a community, AND a DM channel MUST NOT
        # (community_id IS NULL). The first half is #32's original invariant ("a non-DM
        # channel with null community_id is a migration bug"); the second half is the
        # DM-privacy half — without it a buggy/direct writer could store a DM WITH the
        # default Aiko community, and visible_channels_in_community would then leak that
        # DM into the community channel listing (the exact footgun the DM-creation path
        # dodges with null(), now enforced at the DB so no future writer must remember
        # the compensating change). Verified safe vs live prod: every channel is
        # 'standard' with a community (satisfies the non-DM arm); no DM exists yet.
        CheckConstraint(
            "(kind = 'dm' AND community_id IS NULL) "
            "OR (kind != 'dm' AND community_id IS NOT NULL)",
            name="ck_channels_community_required"),
        # A DM MUST be private (#2633, cage-match PR#124 Carnot/Tesla). The DM privacy
        # invariant has TWO DB legs: 'dm' suppresses bus federation (ck_channels_kind +
        # the dual ws gate), AND is_private gates READ access — acl.readable_channel
        # treats is_private=false as world-readable, so a non-private DM would leak to
        # every authed user who knows the id. dm_service always sets is_private=True;
        # this makes "public DM" unrepresentable rather than merely conventional (the
        # same "unrepresentable when wrong" lesson as the community CHECK). is_private
        # is stored 0/1 on SQLite. Non-DM channels are unconstrained here (public
        # channels are legitimately is_private=false).
        CheckConstraint("kind != 'dm' OR is_private = 1",
                        name="ck_channels_dm_private"),
        # The 'dm:' prefix ⟺ kind='dm', BIDIRECTIONAL and CASE-SENSITIVE (#2633,
        # cage-match PR#124 Carnot+Tesla). Completes the DM DB-invariant set (null
        # community, private, dm: prefix). Two legs, both load-bearing:
        #   * kind='dm' ⇒ dm: prefix — the prefix leg of the dual bus gate is guaranteed
        #     present, so a mutator retinting 'dm'->'standard' still can't re-federate.
        #   * a dm: prefix ⇒ kind='dm' — the dm: namespace is TOTALLY reserved at the DB,
        #     so NO writer (create_channel, a direct INSERT, any future path) can squat a
        #     non-DM channel on the private keyspace and block a real pair's DM. This is
        #     the "remove the coupling" version of sealing every aiko_channel writer.
        # substr(...)='dm:' (NOT LIKE): SQLite LIKE case-folds ASCII, so `LIKE 'dm:%'`
        # would admit 'DM:a:b' — which the CASE-SENSITIVE Python gate (is_dm_channel_name
        # / startswith) does NOT recognize, re-opening the exact federation hole the CHECK
        # exists to close. substr comparison is case-sensitive, matching Python's alphabet.
        CheckConstraint(
            "(kind = 'dm' AND substr(aiko_channel, 1, 3) = 'dm:') "
            "OR (kind != 'dm' AND substr(aiko_channel, 1, 3) <> 'dm:')",
            name="ck_channels_dm_prefix"),
        # A DM MUST be invite_only (#2633, cage-match PR#124 Tesla). self_join treats a
        # PRIVATE+OPEN channel as joinable-without-membership; an open-policy DM would let
        # a stranger's POST /join reach the DmMembershipImmutable seal (409) — an existence
        # oracle (409 on a real DM vs 404 on a random id) the seal itself would create. A
        # DM is never join-managed (membership is fixed at creation), so pinning
        # invite_only makes that oracle unrepresentable. dm_service leaves the model
        # default ('invite_only'), so this holds today; the CHECK stops a direct/future
        # write from opening it.
        CheckConstraint("kind != 'dm' OR join_policy = 'invite_only'",
                        name="ck_channels_dm_invite_only"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Closed set (ChannelKind): 'standard' | 'llm' | 'robot' | 'dm' ('group' is NOT yet a
    # member — added with its community-exemption when groups ship; see ChannelKind).
    # Stored as the StrEnum's string value so the column stays a plain VARCHAR; the DB
    # CHECK (ck_channels_kind, above) enforces membership. SECURITY: 'dm' suppresses bus
    # federation (ws.py), so this is a trust-bearing field — the CHECK is what keeps a
    # bad writer from minting a kind that bypasses that gate.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChannelKind.STANDARD)
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
    # The community ("server") this channel belongs to (#32, Phase B1). NULLABLE
    # in the schema so a DM (kind='dm') can be community-less, but the partial
    # CHECK above forbids a NULL on any non-DM channel. The model-level default is
    # the seeded Aiko community, so every channel born from the bus reconcile
    # (upsert_channel) or constructed directly lands in Aiko with no extra code —
    # this is also why existing channels need no per-row decision in the migration.
    # FOOTGUN (sharper than first documented, verified #2633): passing
    # `community_id=None` does NOT bypass this default — SQLAlchemy applies a
    # Python-side scalar `default=` whenever the bound value is None, so `None`
    # SILENTLY stores DEFAULT_COMMUNITY_ID and places the DM in Aiko (→ it would leak
    # into visible_channels_in_community). The DM-creation path (dm_service) uses
    # `sqlalchemy.null()`, the ONLY value that overrides a column default with a real
    # SQL NULL. Any future community-less channel creation must do the same.
    community_id: Mapped[str | None] = mapped_column(
        ForeignKey("communities.id"), nullable=True, index=True,
        default=DEFAULT_COMMUNITY_ID)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    # DB-level role closed-set enforcement beyond the API boundary (#11).
    __table_args__ = (
        CheckConstraint(_in_check("role", Role), name="ck_memberships_role"),
        # USER-centric index (#2633, cage-match PR#124 Carnot). The composite PK is
        # (channel_id, user_id) — leading with channel_id — so a query filtering on
        # user_id alone (GET /v1/dm: "my DM channels", list_dms) cannot use it and would
        # table-scan as memberships grow. This index covers the user-first access path.
        Index("ix_memberships_user_id", "user_id"),
    )
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # member|admin
    can_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Community(Base):
    """A community ("server") that owns channels (#32, Option B / Phase B1).

    Option B introduces a Discord-style nesting: a Community owns many Channels
    (channels.community_id) and users join at the community grain
    (community_memberships). Phase B1 ships the model + a migration that seeds ONE
    default community ("Aiko", id=DEFAULT_COMMUNITY_ID), assigns every existing
    channel to it, and auto-joins every existing user — so the hierarchy exists
    under the hood with NO user-visible change (channels still render flat). The
    discovery/join/list endpoints are Phase B2; user-created communities are
    deferred (seeded/admin-only for now), so there is no creation path yet.

    owner_id is NULLABLE: the seeded default community is SYSTEM-owned (NULL). On
    account deletion the owner link is ANONYMIZED (owner_id -> NULL), NEVER
    cascade-deleted — a community is shared infrastructure like a channel, so
    destroying it on the owner's departure would strip every other member (the
    same reasoning as the message tombstone in accounts_service). The cascade
    guard (test_account_deletion_cascade_guard) enforces that this FK to users.id
    is handled.

    default_channel_id (the channel a joiner lands in) is added in B2 (#32) as a
    PLAIN String column, deliberately NOT a ForeignKey: a real FK
    channels.community_id <-> communities.default_channel_id forms a cycle that
    SQLite create_all cannot order (the reason it was deferred from B1).
    Referential integrity is an application invariant (the value is only ever set
    to a channel that belongs to this community); a dangling id degrades
    gracefully (the app falls back to the first visible channel).
    """
    __tablename__ = "communities"
    __table_args__ = (
        CheckConstraint(_in_check("visibility", Visibility),
                        name="ck_communities_visibility"),
        CheckConstraint(_in_check("category", Category),
                        name="ck_communities_category"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="public")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="general")
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True)
    # Denormalized membership count for the directory projection (#32). Maintained
    # on join/leave in B2; the migration seeds it to the current user count.
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The channel a joiner lands in / the directory highlights (#32, Phase B2).
    # PLAIN String, NOT a ForeignKey — see the class docstring (avoids the
    # channels<->communities FK cycle SQLite create_all cannot order). Nullable: a
    # community with no channels yet has no default.
    default_channel_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True)
    # Rolls up the latest channel activity for directory sort (#32); B2 ships the
    # column but leaves population to a later increment (no per-channel last-ULID
    # rollup yet), so directory sort is `members`/`name` for now. NULL at seed.
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Community-level takedown (#32): moderation can remove a whole community as a
    # unit (channels/messages already have their own takedown). Inert until the B2
    # discovery/join read paths consult it; carried now so the model is complete.
    taken_down_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class CommunityMembership(Base):
    """A user's membership in a community (#32, Phase B1). Mirrors Membership (the
    per-channel join) but at the community grain. Composite PK makes a re-join
    idempotent (one row per (community, user)). No ON DELETE CASCADE — account
    deletion tears these down explicitly (children-before-parent), exactly like
    every other child of users; the cascade guard now requires it."""
    __tablename__ = "community_memberships"
    __table_args__ = (
        CheckConstraint(_in_check("role", Role),
                        name="ck_community_memberships_role"),
    )
    community_id: Mapped[str] = mapped_column(
        ForeignKey("communities.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


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
    # Sovereign-signing envelope (#1816): the client-supplied `origin` object
    # {v, alg, key_version, sender_pubkey, client_msg_id, signed_at_ms, sig},
    # validated for SHAPE at the trust boundary (domain/signing.validate_origin)
    # then carried VERBATIM — the gateway is a carrier, not a verifier. NULL for
    # unsigned messages and every bus-born message (not signed through here);
    # absent origin means "unverified", never "invalid".
    origin: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Key-bound @-mention spans (#2632): a list of discriminated-target spans
    # [{target_type, target_id, offset, length}], SHAPE+caps validated at the
    # trust boundary (domain/mentions.validate_mentions) then carried VERBATIM —
    # the gateway is a pure carrier, not a resolver or filter. `target_id` for a
    # user target is the gateway's OPAQUE user id (the `user_id` the member roster
    # exposes), NOT the raw signing key and never a home-qualified string (ADR-0004:
    # targets key off the opaque identity, so a rename never orphans a mention — the
    # client re-resolves id->current-handle at render). `offset`/`length` index
    # `body` in the client's declared basis (UTF-16) and are round-tripped
    # OPAQUELY; the gateway never re-derives that basis. NULL for a message with
    # no mentions and every bus-born message; message_view omits the key when
    # NULL (absent == "no mentions", mirroring origin's absent == "unverified").
    mentions: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # NEIGHBOR CONSTRAINT for a future edit mutator (#2706): `mentions` offsets index
    # `body`. Any path that REWRITES `body` (an edit endpoint keyed on this column)
    # MUST re-validate or clear `mentions`, or spans dangle into the new text — the
    # same reasoning the account-deletion tombstone applies when it replaces body.
    # No edit route exists yet; this constraint travels with the column so the next
    # edit PR inherits it.
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
        # Closed set for the moderator outcome (Piece B). NULL passes the IN check
        # (SQL NULL IN (...) is NULL, not false), so a pending report is allowed;
        # only a non-null value must be a member of the set.
        CheckConstraint(
            _in_check("resolution", ReportResolution),
            name="ck_message_reports_resolution"),
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
    # Piece B: WHO actioned the report and HOW. Both NULL while pending. The FK is
    # nullable so a moderator's own later account deletion doesn't cascade-destroy
    # the report's audit trail (mirrors reporter_user_id anonymization).
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Retraction(Base):
    """A forward-ULID *retraction* event — the durable, catch-uppable record that a
    message was taken down (#7 takedown propagation).

    WHY a new event and not just ``Message.deleted_at``: the soft-delete mutates a
    row *below* a client's forward watermark. ``get_history`` catch-up is
    ``id > after`` (strictly greater; ``id`` is a monotonic ULID), so a client that
    already synced the message (``id <= last_id``) never re-observes the deletion —
    it would hold a taken-down message forever, and a cold reload of the durable
    on-disk cache doesn't heal it. A retraction is a NEW row with its OWN higher
    ULID that references the taken-down message, so it rides the EXISTING forward
    paths: normal ``get_history`` catch-up (offline-then-reconnect clients) AND WS
    fanout (live clients) — one mechanism, no separate deletions feed, no second
    cursor.

    The retraction IS the durable system of record; WS fanout is a latency
    optimisation over it. It is therefore written in the SAME transaction as the
    soft-delete (``moderation_service.take_down_message``): a delete that committed
    without its retraction would be un-catch-uppable for a client that also missed
    the live frame.

    ``id > target_msg_id`` is guaranteed — ``id`` is minted via ``new_ulid()``
    (time-forward) at takedown, strictly after the target message's own mint, so
    the forward cursor always carries the retraction past any client watermark that
    already includes the target.

    NOT the account-deletion *husk* (``accounts_service``: a deleted user's message
    keeps its slot but has its body/PII wiped, staying visible under a placeholder
    label). A husk REMAINS; a retraction REMOVES. Different mechanism, different
    word, on purpose.
    """
    __tablename__ = "message_retractions"
    __table_args__ = (
        # The forward-catch-up query filters channel_id then ranges/orders on id
        # (`WHERE channel_id=? AND id > ? ORDER BY id`); a COMPOSITE (channel_id, id)
        # index matches that access path exactly (cage-match Carnot) — sharper than a
        # channel_id-only index leaning on the global PK order across all channels.
        Index("ix_message_retractions_channel_id_id", "channel_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    # The taken-down message. The row survives the soft-delete (and the account-
    # deletion husk keeps rows too), so this FK target is stable. Indexed for the
    # per-message dedup lookup ("is this message already retracted?"). NOTE: there is
    # deliberately NO block join on retractions anywhere — a delete carries no content,
    # so it is never block-filtered (#7 add/remove asymmetry). Do not add one; it would
    # strand takedowns across a block/unblock epoch.
    target_msg_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id"), nullable=False, index=True)
    # Scopes the retraction to a channel so get_history pages it on the same
    # (channel_id, id) axis as messages — covered by the composite index above.
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("channels.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


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


class SigningKey(Base):
    """An observed pubkey->account binding for sovereign message signing (#1816 PR B).

    Records the fact "authenticated account ``user_id`` presented Ed25519 public
    key ``pubkey`` at send time" — the contemporaneous binding that elevates an
    echoed ``origin`` from *forgery-as-echo* (a sig proves *a* key signed these
    bytes) toward *whose* key an account uses. Written two ways through ONE door
    (``signing_keys_service.record_signing_key``): IMPLICITLY inside
    ``create_outbound`` whenever a signed message is sent, and EXPLICITLY via
    ``POST /v1/keys``. Both are the same idempotent upsert.

    UNIQUENESS IS PER-USER, NOT GLOBAL — a deliberate carrier-semantics choice,
    not an oversight. The gateway is a CARRIER, not a verifier: it never checks a
    signature, so when account A presents ``pubkey``, all it *knows* is "A (authed
    via its session) used this key", NEVER "this key belongs to A". A `UNIQUE`
    constraint must encode a fact the system can actually know, so the key is
    ``(user_id, pubkey)`` — dedupe the observation A actually made — not
    ``(pubkey)``, which would assert single-account ownership the carrier cannot
    establish. Enforcing global uniqueness here would be actively wrong:

      * The pubkey is PUBLIC (it rides in every echoed message). With no
        proof-of-possession, global-unique degrades to "first account to *present*
        the key owns it" — so an attacker who merely SAW Alice's key could race to
        register it first, locking Alice out and notarizing the impostor's binding
        with DB authority.
      * The security-interesting event — two accounts presenting the same key —
        is a SIGNAL a future trust root should adjudicate. Per-user storage keeps
        BOTH observations (``(A, k)`` and ``(B, k)``, timestamped) as durable
        evidence; a global `UNIQUE` would destroy the collision as an
        ``IntegrityError`` at write time, decided by a layer with no basis to say
        which account is the impostor.
      * It would be a fail-closed griefing primitive (register a victim's public
        key first → their legitimate key-record fails) that stops nothing (the
        attacker can still stuff any pubkey into a message frame).

    Global uniqueness becomes correct — and this stance flips — ONLY once key
    REGISTRATION gains proof-of-possession (a challenge the caller signs with the
    private key). That trust root is deferred (#1816 T-series / federation #1760).
    Until it exists, per-user is the only HONEST model — exactly as strong as the
    carrier's real knowledge, no stronger (cf. transport-vs-trust-boundary).

    Cross-user pollution is therefore harmless and self-labeling: an attacker can
    record ``(attacker, victim_pubkey)``, but it is filed under the ATTACKER's
    account, never touches the victim's row, and can never let them send AS the
    victim (sends bind ``sender_user_id`` to the authed session server-side —
    invariant I5).

    EVIDENCE DURABILITY IS SCOPED TO LIVE KEYS (cage-match Tesla): the collision
    signal above persists only while both rows exist. User-facing revoke
    (``signing_keys_service.revoke_key``) HARD-deletes, so a caller can erase their
    own ``(caller, pubkey)`` observation — the roster is a live-key ledger, not an
    append-only audit log. This is acceptable pre-trust-root because nothing
    adjudicates collisions yet; a retained-evidence soft-revoke (``revoked_at``
    tombstone) + a non-unique index on ``pubkey`` to make the collision query cheap
    are part of the revocation/rotation lifecycle explicitly deferred to federation
    #1760. Until then, treat this table as a notarized ledger of live presentations,
    NOT proof of possession.

    No ON DELETE CASCADE (codebase convention): account deletion tears these down
    explicitly (children-before-parent) via ``signing_keys_service.purge_user_keys``;
    the cascade guard (``test_account_deletion_cascade_guard``) requires it.
    """
    __tablename__ = "signing_keys"
    # Named constraint (not column-level) so the ORM metadata matches the
    # hand-written 0012 migration EXACTLY — the parity gate diffs reflected
    # unique constraints and an unnamed one would not match the named migration.
    __table_args__ = (
        UniqueConstraint("user_id", "pubkey", name="uq_signing_keys_user_pubkey"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    # The multibase-base58btc ed25519 Multikey string (`z…`) exactly as it rides in
    # `origin.sender_pubkey`. 128 matches signing._MAX_PUBKEY_STR (a real Multikey
    # is ~48 chars; the cap is defense-in-depth). Stored as the wire string — human
    # inspectable and the exact value a verifier feeds `decode_multikey`.
    pubkey: Mapped[str] = mapped_column(String(128), nullable=False)
    # The app's announced key version for this pubkey (>= 1). Carried for the future
    # rotation/revocation lifecycle; a fixed pubkey keeps its first-seen version.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


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
    credential_id, verifies the assertion against public_key, applies the spec
    sign_count clone-detection rule (a non-increase is clone evidence ONLY when the
    counts are nonzero — platform authenticators report 0 and never increment, so
    0/0 is permitted; delegated to py_webauthn), persists the returned count, then
    issues a session for user_id.

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


class RecoveryPolicy(Base):
    """A user's social-recovery policy — the k-of-n quorum threshold (Design 05).

    ONE active policy per user (UNIQUE user_id); a rotation REPLACES it (through the
    veto machinery — see recovery_service.enroll_policy). The gateway stores only the
    threshold here; the n approver public keys are RecoveryApprover rows. Neither
    holds anything that can recover an account alone — a leaked DB has verifier keys
    (they check signatures, they cannot PRODUCE them) + a threshold, never a signing
    secret. Contrast SigningKey (#1816): that pubkey is a CARRIER the gateway never
    verifies against (per-user non-unique, collisions kept as evidence); an approver
    pubkey is the mirror image — the gateway DOES verify against it, a quorum of them
    is authoritative-for-takeover, so the roster is registered authenticated and
    DISTINCT (UNIQUE(user_id, approver_pubkey) on RecoveryApprover).

    No ON DELETE CASCADE (codebase convention): account deletion tears these down
    explicitly via recovery_service.purge_user_recovery; the cascade guard
    (test_account_deletion_cascade_guard) requires it.
    """
    __tablename__ = "recovery_policies"
    # Named constraint (not column-level unique=True) so the ORM metadata matches
    # the hand-written 0013 migration EXACTLY — the parity gate diffs reflected
    # unique constraints and an unnamed one would not match the named migration.
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_recovery_policies_user"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False)
    # The quorum size: how many DISTINCT approver signatures a recovery needs. The
    # service enforces 1 <= threshold_k <= n at enroll time; stored as-is.
    threshold_k: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class RecoveryApprover(Base):
    """One guardian's Ed25519 approver PUBLIC key registered for a user's recovery
    policy (Design 05). The guardian holds the private half (generated in-app, never
    delivered to the island); the gateway stores only the public key and VERIFIES
    approval signatures against it. Approver keys are independent of any aiko account
    — a guardian need not be an aiko user — so the roster is opaque (keys, not
    identities).

    UNIQUE(user_id, approver_pubkey) is the DISTINCT-approver invariant at the schema
    level: one guardian's key can be registered against a user only once, so a quorum
    of >= k rows is >= k distinct guardians (the service also de-dupes at verify time
    on the presented signatures, closing the "one guardian signs k times" attack).

    No ON DELETE CASCADE (codebase convention): purged via
    recovery_service.purge_user_recovery on account deletion; the cascade guard
    requires it.
    """
    __tablename__ = "recovery_approvers"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "approver_pubkey", name="uq_recovery_approvers_user_pubkey"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    # The multibase-base58btc ed25519 Multikey string (`z…`) — same wire format as
    # SigningKey.pubkey / origin.sender_pubkey. 128 matches signing._MAX_PUBKEY_STR
    # (a real Multikey is ~48 chars; the cap is defense-in-depth).
    approver_pubkey: Mapped[str] = mapped_column(String(128), nullable=False)
    # A client-side hint only ("mum's phone"), opaque to the gateway. Nullable.
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class PendingRecovery(Base):
    """An in-flight recovery in the time-locked veto window (Design 05 §7).

    Created ONLY after a valid quorum (recover/finish), staging the new passkey
    credential with a write-once veto_deadline. Existing devices are notified and can
    cancel during the window; after the deadline the client polls finalize, which is a
    single guarded DELETE with the deadline folded into the WHERE (never observe-then-
    write) — the anti-TOCTOU contract the whole design rests on (§7 fixes C2).

    UNIQUE(user_id): one in-flight recovery per user. A griefer cannot weaponize the
    slot into a lockout — a pending row is created only after a valid quorum, and an
    EXPIRED-unfinalized row is reclaimable by a fresh recovery (recovery_service
    .start/finish reclaim on expiry), so it can't wedge the slot past its window.

    finalize_token_hash stores sha256(finalize_token) — the raw high-entropy token is
    returned to the caller once at finish and NEVER stored (the ULID id is ordered,
    not secret, so it can't be the finalize authorizer alone).

    No ON DELETE CASCADE (codebase convention): purged via
    recovery_service.purge_user_recovery on account deletion; the cascade guard
    requires it.
    """
    __tablename__ = "pending_recovery"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_pending_recovery_user"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False)
    # The staged WebAuthn credential (verified attestation, not yet live). Mirrors
    # PasskeyCredential's column shapes so finalize can enroll it through the single
    # passkey door without any type coercion.
    staged_credential_id: Mapped[str] = mapped_column(String(512), nullable=False)
    staged_public_key: Mapped[str] = mapped_column(Text, nullable=False)  # base64url(COSE)
    staged_sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Server wall-clock (_utcnow) deadline, WRITE-ONCE — set at row birth, never
    # advanced (a repeated finish can't push the window out; §7). The finalize
    # DELETE folds `veto_deadline <= :now` into its WHERE.
    veto_deadline: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    # sha256 hex of the high-entropy finalize token (never the raw token).
    finalize_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class MessageReaction(Base):
    """One user's SIGNED emoji reaction to one message (#2634, v2 social layer).

    IDENTITY-BEARING + SIGNED FROM DAY ONE (rebuild after the reverted anonymous
    first cut). A reaction is a *signed lightweight endorsement* — raw material for
    the Carried Record (#2506) — so it carries the reactor's ``user_id`` (exposed on
    read, NOT an anonymous tally) and its Ed25519 ``origin`` envelope, captured at
    birth (you cannot sign history retroactively). The gateway CARRIES the envelope,
    it does not verify it (identical posture to a signed message — see
    ``signing.validate_origin`` and the message ``origin`` column); an absent/garbage
    origin reads as "unverified", never "invalid" (a legacy/degraded client may react
    unsigned and is still carried). See
    ``docs/crucible/sovereign-reaction-signing/SIGNING-SPEC.md``.

    STATE, NOT EVENT — the deliberate contrast with ``Retraction`` (#7). A
    retraction needs its own forward-ULID row because a takedown mutates a message
    *below* a client's watermark, and ``get_history`` catch-up (``id > after``) would
    never replay it. A reaction changes an *aggregate* that ``message_view``
    recomputes on every history read, so a client that misses the live ``reaction``
    frame self-heals the moment it re-pages that message — no second event feed, no
    forward-ULID row, just this table. (A signed ``remove`` is authorised by row
    OWNERSHIP here, not persisted as a second signed event; if a reputation trail ever
    needs the signed-remove history, that is the additive forward-ULID upgrade, exactly
    like ``Retraction``.)

    NAMED TRADEOFF (state-not-event, cage-match this): a reaction add/remove on a
    message a client has ALREADY synced is not force-caught-up — the live ``reaction``
    frame is best-effort and the ``id > after`` cursor doesn't advance for a reaction
    (it mints no id on the message axis). It self-heals only when the client re-reads
    that message ROW — scroll-up ``before`` paging, a cold reload, or a re-bind that
    re-fetches history. NOT "on reconnect": a client that keeps synced messages
    resident and only forward-pages from its watermark never re-reads the row, so its
    aggregate stays frozen until it re-fetches. Ambient-signal eventual consistency
    (Slack/Discord reactions behave the same).

    COMPOSITE PK ``(message_id, user_id, emoji)`` makes a repeat-react idempotent —
    one row per (message, user, emoji), the same one-row-per-relationship shape as
    ``Membership`` / ``CommunityMembership``, AND exactly the SIGNING-SPEC idempotency
    key ``(user, target_msg_id, exact-emoji-bytes)`` (the emoji is stored as the exact
    UTF-8 the client signed, never normalised — the spec's canonicalization rule). The
    reaction's OWN ``client_msg_id`` (signing-bytes field #4) rides INSIDE ``origin``;
    it is not a separate key, since the PK already pins uniqueness. Re-adding the same
    emoji is a no-op (INSERT-or-ignore in ``reactions_service``); a different emoji from
    the same user is a NEW row. The PK's leading ``message_id`` covers the aggregation
    read (``WHERE message_id IN (...) GROUP BY message_id, emoji``); ``user_id`` is
    separately indexed for the account-deletion purge and the per-viewer
    ``reacted_by_me`` probe.

    ``emoji`` is an OPAQUE client string (the gateway never renders or normalises it),
    bounded to 64 chars as defense-in-depth — a real emoji, incl. a ZWJ sequence
    (family, skin-tone, flag) is well under that; the cap just stops an unbounded blob
    masquerading as an emoji (mirrors the pubkey-length caps elsewhere).

    ``origin`` is the shape-validated sovereign-signing envelope (JSON), NULL for an
    unsigned reaction — identical column semantics to ``Message.origin``.

    No ON DELETE CASCADE (codebase convention): account deletion tears these down
    explicitly via ``reactions_service.purge_user_reactions`` (the cascade guard
    requires it), and channel hard-delete tears them down before its messages (they
    FK ``messages.id`` — verify-the-neighbor, like ``MessageReport`` in
    ``channels_service.hard_delete_channel``).

    Message SOFT-DELETE (takedown, #7) deliberately does NOT purge reactions — the
    same reason it preserves the message body/row: a soft-delete is REVERSIBLE, so its
    children are preserved-and-hidden, not destroyed (a reversed takedown restores the
    reactions with the message). The reactions are inert while hidden — ``get_history``
    never returns a ``deleted_at`` row, so its reactions never surface in an aggregate,
    and the visibility gate refuses a NEW reaction on a soft-deleted message. Only the
    two IRREVERSIBLE deletes (channel hard-delete, account deletion) purge.
    """
    __tablename__ = "message_reactions"
    __table_args__ = (
        # The composite PK IS the (message_id, user_id, emoji) uniqueness — declared
        # here (not column-level) so the ORM metadata matches the hand-written 0018
        # migration exactly (the parity gate diffs reflected constraints).
        PrimaryKeyConstraint(
            "message_id", "user_id", "emoji", name="pk_message_reactions"),
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True)
    emoji: Mapped[str] = mapped_column(String(64), nullable=False)
    # Sovereign-signing envelope (#1816/#2634): the client-supplied `origin` object,
    # shape-validated at the trust boundary (domain/signing.validate_origin) and echoed
    # verbatim on read so a client can verify the endorsement. NULL for an unsigned
    # reaction — an absent origin reads as "unverified", never "invalid".
    # none_as_null=True persists an unsigned reaction as SQL NULL (not the JSON text
    # 'null'), so the add_reaction upgrade guard `WHERE origin IS NULL` correctly
    # matches an unsigned row and can upgrade it to signed (cage-match Tesla r2 P1).
    origin: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
