"""Agent identities + the OIDC agent-ingress (Citizenship for the Dreaming, H1).

Two doors, both sealed here (the mutator, not the caller — CLAUDE.md single-door):

  * ADMIN provisioning — ``create_agent_binding``: an island admin maps a GitHub
    Actions workload (repository, ref, workflow_ref) + an expected ``aud`` to a NEW
    agent identity (a ``kind='agent'`` User created in the SAME transaction). This is
    the ONLY place a ``kind='agent'`` row is ever born, so no request field can
    self-promote a human.

  * OIDC authentication — ``authenticate_agent_oidc``: a caller presents a GitHub
    Actions OIDC token; we verify its SIGNATURE (github_oidc), match the verified
    claims to a binding, check the token ``aud`` against the binding's declared aud,
    apply the ban gate, and return the agent User. The REST layer then mints a
    short-lived aiko ACCESS token (no refresh — an agent re-auths via a fresh OIDC
    token every run, so there is no long-lived credential to steal or revoke).

Revocation is the ORDINARY moderation ban on the agent's User row (``banned_at``):
``is_banned`` here blocks new mints, and the WS handshake + ``hub.disconnect_user``
drop a live socket — one mechanism shared with humans, no separate agent kill-switch.
"""
from __future__ import annotations

import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import github_oidc, users_service
from .ids import new_ulid
from .models import AgentBinding, Kind, User


# The UNIQUE constraint on (repository, ref, workflow_ref) — the single source of the
# name is models.AgentBinding.__table_args__; kept in sync here for portable
# classification (see _is_binding_conflict).
_BINDING_UNIQUE_CONSTRAINT = "uq_agent_bindings_repo_ref_workflow"


def _is_binding_conflict(err: IntegrityError) -> bool:
    """True iff the IntegrityError is THIS binding's triple-UNIQUE violation, classified
    ENGINE-PORTABLY rather than by a free-text message substring:

      * SQLSTATE ``23505`` (unique_violation) is the standard code every DB-API driver
        that reports SQLSTATE uses (Postgres via psycopg's ``sqlstate``/``pgcode``); it
        distinguishes a UNIQUE violation from any other IntegrityError (FK/NOT NULL) on
        ANY such engine. Postgres also names the CONSTRAINT in the error, so we confirm
        it is our triple index, not some other future unique constraint on the table.
      * SQLite (aiosqlite, dev+prod today, CLAUDE.md) exposes NO SQLSTATE and names the
        COLUMNS not the constraint, so we fall back to matching the constrained columns
        — this is the only path that must read the message, and only when there is no
        SQLSTATE to key on. The explicit get_binding pre-check already handles the
        non-racing duplicate on every engine; this backstop only fires on a true
        concurrent insert, and now returns 409 (not an opaque 500) on Postgres too."""
    orig = getattr(err, "orig", err)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    text = str(orig)
    if sqlstate is not None:
        # An engine that reports SQLSTATE: a unique_violation naming our constraint.
        return sqlstate == "23505" and _BINDING_UNIQUE_CONSTRAINT in text
    # SQLite fallback: no SQLSTATE; the message enumerates the constrained columns.
    return (
        "agent_bindings.repository" in text
        and "agent_bindings.ref" in text
        and "agent_bindings.workflow_ref" in text
    )


class BindingConflict(Exception):
    """A binding for this (repository, ref, workflow_ref) already exists. The caller
    maps this to 409 — a workload maps to exactly one agent (UNIQUE on the triple)."""


class InvalidBinding(Exception):
    """A create_agent_binding argument is empty/blank. The caller maps this to 422 —
    a blank aud would degrade the door to 'accept any audience', and a blank
    repository/ref/workflow_ref could never match a real token."""


class AgentNotBound(Exception):
    """A signature-valid OIDC token whose (repository, ref, workflow_ref) matches NO
    binding. The caller maps this to 401 — same opaque rejection as a bad token (no
    existence leak about which workloads are provisioned)."""


class AgentBindingDangling(Exception):
    """The matched binding points at a user_id that no longer exists, OR the resolved
    user is not kind='agent' (should both be impossible — the agent User is created in
    one txn with the binding and kind is server-authoritative). Fail closed: a data-
    drift / FK anomaly must never mint a token, so this maps to the same opaque 401."""


class AgentBanned(Exception):
    """The resolved agent identity is suspended (``banned_at`` set). Raised INSIDE the
    auth mutator (not only at the REST layer) so no caller — a future internal path
    included — can mint a token for a banned agent by skipping the REST ban gate. The
    caller maps this to 403 account_suspended, the same treatment a banned human gets."""


async def create_agent_binding(
    session: AsyncSession, *, repository: str, repository_id: str,
    repository_owner_id: str, ref: str, workflow_ref: str,
    aud: str, display_name: str = "",
) -> tuple[User, AgentBinding]:
    """Provision a NEW agent identity bound to a GitHub Actions workload. Admin-gated
    at the REST layer. Creates a ``kind='agent'`` User (auto handle ``agent-<hex>``,
    cosmetic — an agent authenticates by OIDC, never by name) and the binding row in
    ONE transaction, so a binding never dangles. Raises ``InvalidBinding`` on a blank
    field and ``BindingConflict`` if the triple is already bound.

    ``repository_id`` / ``repository_owner_id`` are GitHub's IMMUTABLE numeric ids for
    the repo and its owner (the admin reads them from the repo's API/OIDC sample). The
    OIDC door authorizes on THESE, not on the mutable ``repository`` name — so a
    recycled or transferred repo slug can never mint this identity."""
    repository = (repository or "").strip()
    repository_id = (repository_id or "").strip()
    repository_owner_id = (repository_owner_id or "").strip()
    ref = (ref or "").strip()
    workflow_ref = (workflow_ref or "").strip()
    aud = (aud or "").strip()
    if not (repository and repository_id and repository_owner_id
            and ref and workflow_ref and aud):
        raise InvalidBinding(
            "repository, repository_id, repository_owner_id, ref, workflow_ref, and "
            "aud are all required and non-blank")

    # PORTABLE duplicate detection: an explicit pre-check that returns BindingConflict
    # (→ 409) BEFORE the insert, independent of any engine's error-string format. The
    # try/except below stays as the RACE backstop for two concurrent creates that both
    # pass this check; it classifies portably (SQLSTATE + constraint name, SQLite column
    # fallback — see _is_binding_conflict). The pre-check is what keeps an ordinary
    # duplicate from becoming a 500 on Postgres; the backstop now covers the race too.
    existing = await get_binding(
        session, repository=repository, ref=ref, workflow_ref=workflow_ref)
    if existing is not None:
        raise BindingConflict()

    # A fresh agent handle. agent-<12 hex> against a near-empty users table — a genuine
    # username/aiko_username collision is vanishingly unlikely, and the UNIQUE
    # constraint would surface it as an IntegrityError classified below.
    handle = f"agent-{secrets.token_hex(6)}"
    user = User(
        id=new_ulid(),
        kind=Kind.AGENT,
        username=handle,
        display_name=display_name or handle,
        password_hash=None,  # an agent has no password; it authenticates by OIDC
        aiko_username=users_service._sanitize_aiko_username(handle),
        email=None,
    )
    binding = AgentBinding(
        id=new_ulid(),
        repository=repository,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        ref=ref,
        workflow_ref=workflow_ref,
        aud=aud,
        user_id=user.id,
    )
    session.add(user)
    session.add(binding)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        # RACE backstop (the pre-check above handles the ordinary duplicate). Classify
        # ENGINE-PORTABLY by SQLSTATE (23505) + constraint name on engines that report
        # it, falling back to the SQLite column message only when there is no SQLSTATE
        # — so a future Postgres concurrent insert becomes a clean 409, not an opaque
        # 500. This path only fires on a true concurrent insert (the pre-check above
        # caught every non-racing duplicate on every engine).
        if _is_binding_conflict(e):
            raise BindingConflict() from e
        raise
    return user, binding


async def get_binding(
    session: AsyncSession, *, repository: str, ref: str, workflow_ref: str,
) -> AgentBinding | None:
    """The binding for an exact (repository, ref, workflow_ref) triple, or None."""
    return (await session.execute(
        select(AgentBinding).where(
            AgentBinding.repository == repository,
            AgentBinding.ref == ref,
            AgentBinding.workflow_ref == workflow_ref,
        )
    )).scalar_one_or_none()


def _aud_matches(token_aud: str | list[str], expected: str) -> bool:
    """Whether the token's ``aud`` claim satisfies the binding's declared audience.
    The JWT ``aud`` may be a string or an array; the binding is satisfied iff the
    expected audience appears. Constant-time per candidate (aud is not secret, but the
    codebase compares auth-relevant strings this way — no reason to leak here)."""
    candidates = [token_aud] if isinstance(token_aud, str) else list(token_aud)
    exp = expected.encode("utf-8")
    return any(
        isinstance(c, str) and hmac.compare_digest(c.encode("utf-8"), exp)
        for c in candidates
    )


async def authenticate_agent_oidc(session: AsyncSession, oidc_token: str) -> User:
    """THE OIDC agent door. Verify the GitHub Actions token, match a binding, check
    the audience, apply the ban gate, and return the agent User. Raises:
      * github_oidc.InvalidOidcToken / OidcProviderUnavailable — bad token (401) /
        GitHub outage (503), propagated as-is.
      * AgentNotBound — signature-valid token for an unprovisioned workload, OR an
        audience mismatch (401, opaque — the two are indistinguishable to the caller
        so a probing token learns nothing about which workloads/audiences exist).
      * users_service.is_banned → SessionBanned-style handling at the caller.
    Does NOT mint a token (the REST layer owns that, carrying the user's live
    token_generation)."""
    claims = await github_oidc.verify_github_oidc_token(oidc_token)

    binding = await get_binding(
        session, repository=claims.repository, ref=claims.ref,
        workflow_ref=claims.workflow_ref)
    if binding is None:
        raise AgentNotBound()
    # IMMUTABLE-identity gate: the mutable repo NAME located the binding, but a rename /
    # transfer / slug-recycle can point a DIFFERENT repo at that name. Authorize on the
    # signature-verified numeric ids GitHub never reuses. A mismatch collapses into the
    # SAME opaque AgentNotBound as an absent binding (no oracle about what's provisioned).
    # constant-time compare: not secret, but the codebase compares auth strings this way.
    if not (hmac.compare_digest(claims.repository_id, binding.repository_id)
            and hmac.compare_digest(
                claims.repository_owner_id, binding.repository_owner_id)):
        raise AgentNotBound()
    # Audience: the token must carry the aud the operator declared for this binding.
    # A mismatch is collapsed into the SAME AgentNotBound as an absent binding — a
    # token minted for a different aud in a bound repo learns nothing (no oracle).
    if not _aud_matches(claims.aud, binding.aud):
        raise AgentNotBound()

    user = await users_service.get_by_id(session, binding.user_id)
    if user is None:
        raise AgentBindingDangling()
    # Defense-in-depth vs FK/data drift: the resolved row MUST be an agent. The binding
    # only ever points at a kind='agent' User (created in one txn), so a non-agent here
    # means a corrupted/re-pointed row — fail closed rather than mint an aiko token for
    # a human identity via the agent door.
    if user.kind != Kind.AGENT:
        raise AgentBindingDangling()
    # Ban gate INSIDE the mutator (not only at REST): a suspended agent is denied a
    # fresh token here, so no caller — REST today, a future internal path tomorrow —
    # can mint by skipping the REST ban check. Same banned_at the human ban machinery
    # sets; the caller maps AgentBanned to 403 account_suspended.
    if users_service.is_banned(user):
        raise AgentBanned()
    return user
