"""Agent-ingress endpoints (Citizenship for the Dreaming, H1 — claude-tasks#2403).

Two doors over ``agents_service``:

  * ``POST /v1/agents/bindings`` — gated behind the DEDICATED AGENT_BINDING_ADMINS
    allowlist (require_agent_binding_admin), NOT the content-moderator set: minting a
    production agent identity is a higher-privilege act than moderation. "Config, not a
    role system" (fail-closed empty), mirroring require_moderator's shape.
    Provisions a new ``kind='agent'`` identity bound to a GitHub Actions workload.

  * ``POST /v1/agents/token`` — PUBLIC (the GitHub Actions OIDC token IS the
    credential; there is no prior aiko session). Rate-limited like the other public
    auth ceremonies. Verifies the OIDC token, matches a binding, and mints a
    SHORT-LIVED aiko ACCESS token (no refresh — an agent re-auths via a fresh OIDC
    token each run, so no long-lived credential is ever issued or stored).

This door NEVER creates a user (agents are admin-provisioned at binding time) and
NEVER touches ``open_registration`` — it cannot be a self-onboarding bypass.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..config import settings
from ..domain import agents_service, github_oidc, security
from ..domain.rate_limit import rate_limit
from . import auth as auth_routes  # reuse the single ban-at-mint door (_deny_if_banned)
from .deps import AgentBindingAdmin, DbSession
from .errors import AccountSuspended

router = APIRouter(prefix="/v1/agents", tags=["agents"])

log = logging.getLogger(__name__)


class CreateBindingReq(BaseModel):
    # GitHub OIDC claims the binding matches on (all signature-protected in the token).
    repository: str = Field(min_length=1)          # "owner/repo" (mutable NAME)
    # IMMUTABLE numeric ids GitHub never reuses — the authorization root (the name only
    # locates the binding). The admin reads these off the repo (e.g. the GitHub API
    # `id` / `owner.id`, or a sample OIDC token's repository_id/repository_owner_id).
    repository_id: str = Field(min_length=1)       # e.g. "638902173"
    repository_owner_id: str = Field(min_length=1)  # e.g. "9919"
    ref: str = Field(min_length=1)                 # "refs/heads/main"
    workflow_ref: str = Field(min_length=1)        # "owner/repo/.github/workflows/x.yml@ref"
    aud: str = Field(min_length=1)                 # the audience the workflow will request
    display_name: str = ""


class AgentTokenReq(BaseModel):
    # The GitHub Actions OIDC token (core.getIDToken(aud)). The credential itself.
    oidc_token: str = Field(min_length=1)


@router.post("/bindings", status_code=status.HTTP_201_CREATED)
async def create_binding(
    req: CreateBindingReq, admin: AgentBindingAdmin, session: DbSession,
) -> dict:
    """Admin provisions a new agent identity bound to a GitHub Actions workload.
    Gated behind the AGENT_BINDING_ADMINS allowlist (see require_agent_binding_admin)
    — minting a PRODUCTION agent identity is a higher-privilege act than content
    moderation, so it does NOT reuse the moderator set. 409 if the (repository, ref,
    workflow_ref) triple is already bound; 422 on a blank field."""
    try:
        user, binding = await agents_service.create_agent_binding(
            session,
            repository=req.repository, repository_id=req.repository_id,
            repository_owner_id=req.repository_owner_id,
            ref=req.ref, workflow_ref=req.workflow_ref,
            aud=req.aud, display_name=req.display_name)
    except agents_service.InvalidBinding as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except agents_service.BindingConflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a binding for this repository/ref/workflow already exists")
    log.info("agent binding created by admin=%s agent_user=%s repo=%s repo_id=%s",
             admin.id, user.id, binding.repository, binding.repository_id)
    return {
        "binding_id": binding.id,
        "user_id": user.id,
        "username": user.username,
        "kind": user.kind,
        "repository": binding.repository,
        "repository_id": binding.repository_id,
        "repository_owner_id": binding.repository_owner_id,
        "ref": binding.ref,
        "workflow_ref": binding.workflow_ref,
        "aud": binding.aud,
    }


@router.post("/token", dependencies=[rate_limit("agent")])
async def mint_agent_token(req: AgentTokenReq, session: DbSession) -> dict:
    """Exchange a GitHub Actions OIDC token for a short-lived aiko access token.

    401 for any bad/unbound/wrong-audience token (opaque — no oracle about which
    workloads are provisioned); 503 if GitHub's JWKS is unreachable; 403 if the bound
    agent has been suspended (the ban gate, via the shared _deny_if_banned door)."""
    try:
        user = await agents_service.authenticate_agent_oidc(session, req.oidc_token)
    except github_oidc.OidcProviderUnavailable:
        # GitHub/JWKS outage — OUR-side transient, not a bad credential. 503 so clients
        # don't retry-storm an auth failure that isn't theirs.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "identity provider unavailable")
    except agents_service.AgentBanned:
        # The bound agent is suspended. The ban gate now lives INSIDE the mutator
        # (defense-in-depth: no caller can skip it), so it surfaces as this dedicated
        # exception → 403 account_suspended, the same treatment a banned human gets.
        raise AccountSuspended()
    except (github_oidc.InvalidOidcToken, agents_service.AgentNotBound,
            agents_service.AgentBindingDangling):
        # Bad signature/alg/iss/exp, missing claim, unprovisioned workload, wrong
        # repo-id/audience, or a dangling/non-agent binding — all collapse to one
        # opaque 401.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or unbound token")

    # Belt-and-suspenders ban gate (the mutator above is now authoritative and already
    # raised AgentBanned for a suspended agent; this keeps the mint door inside the
    # #1927 _deny_if_banned enumeration in case the service contract ever changes).
    auth_routes._deny_if_banned(user)

    # Short-lived ACCESS token only — no refresh. An agent presents a fresh OIDC token
    # every run, so it never needs a long-lived aiko credential. gen carries the user's
    # live token_generation so a generation bump revokes it like any session.
    return {
        "access_token": security.issue_access(user.id, gen=user.token_generation),
        "token_type": "bearer",
        "expires_in": settings.jwt_access_ttl_seconds,
        "user_id": user.id,
    }
