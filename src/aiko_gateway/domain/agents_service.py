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
    """The matched binding points at a user_id that no longer exists (should be
    impossible — created in one txn, no agent-deletion path yet). Fail closed."""


async def create_agent_binding(
    session: AsyncSession, *, repository: str, ref: str, workflow_ref: str,
    aud: str, display_name: str = "",
) -> tuple[User, AgentBinding]:
    """Provision a NEW agent identity bound to a GitHub Actions workload. Admin-gated
    at the REST layer. Creates a ``kind='agent'`` User (auto handle ``agent-<hex>``,
    cosmetic — an agent authenticates by OIDC, never by name) and the binding row in
    ONE transaction, so a binding never dangles. Raises ``InvalidBinding`` on a blank
    field and ``BindingConflict`` if the triple is already bound."""
    repository = (repository or "").strip()
    ref = (ref or "").strip()
    workflow_ref = (workflow_ref or "").strip()
    aud = (aud or "").strip()
    if not (repository and ref and workflow_ref and aud):
        raise InvalidBinding(
            "repository, ref, workflow_ref, and aud are all required and non-blank")

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
        # Classify from the SQLite message, which names the COLUMN(S), not the
        # constraint name: a triple collision reads "UNIQUE constraint failed:
        # agent_bindings.repository, agent_bindings.ref, agent_bindings.workflow_ref"
        # (mirrors users_service._is_credential_id_conflict's table.column match).
        # SQLite is the sole engine in dev AND prod (CLAUDE.md), so this is safe. A
        # users.username/aiko_username auto-handle clash names a DIFFERENT table, so
        # it won't match here and re-raises — the admin retries (astronomically rare).
        if "agent_bindings.repository" in str(getattr(e, "orig", e)):
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
    # Audience: the token must carry the aud the operator declared for this binding.
    # A mismatch is collapsed into the SAME AgentNotBound as an absent binding — a
    # token minted for a different aud in a bound repo learns nothing (no oracle).
    if not _aud_matches(claims.aud, binding.aud):
        raise AgentNotBound()

    user = await users_service.get_by_id(session, binding.user_id)
    if user is None:
        raise AgentBindingDangling()
    return user
