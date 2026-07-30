"""Agent-ingress endpoints (Citizenship for the Dreaming, H1 — claude-tasks#2403).

Two doors over ``agents_service``:

  * ``POST /v1/agents/bindings`` — ADMIN-gated (the island moderator set, the closest
    existing operator role; "config, not a role system", mirroring require_moderator).
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
from .deps import DbSession, ModeratorUser

router = APIRouter(prefix="/v1/agents", tags=["agents"])

log = logging.getLogger(__name__)


class CreateBindingReq(BaseModel):
    # GitHub OIDC claims the binding matches on (all signature-protected in the token).
    repository: str = Field(min_length=1)          # "owner/repo"
    ref: str = Field(min_length=1)                 # "refs/heads/main"
    workflow_ref: str = Field(min_length=1)        # "owner/repo/.github/workflows/x.yml@ref"
    aud: str = Field(min_length=1)                 # the audience the workflow will request
    display_name: str = ""


class AgentTokenReq(BaseModel):
    # The GitHub Actions OIDC token (core.getIDToken(aud)). The credential itself.
    oidc_token: str = Field(min_length=1)


@router.post("/bindings", status_code=status.HTTP_201_CREATED)
async def create_binding(
    req: CreateBindingReq, moderator: ModeratorUser, session: DbSession,
) -> dict:
    """Admin provisions a new agent identity bound to a GitHub Actions workload.
    409 if the (repository, ref, workflow_ref) triple is already bound; 422 on a
    blank field."""
    try:
        user, binding = await agents_service.create_agent_binding(
            session,
            repository=req.repository, ref=req.ref, workflow_ref=req.workflow_ref,
            aud=req.aud, display_name=req.display_name)
    except agents_service.InvalidBinding as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except agents_service.BindingConflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a binding for this repository/ref/workflow already exists")
    log.info("agent binding created by moderator=%s agent_user=%s repo=%s",
             moderator.id, user.id, binding.repository)
    return {
        "binding_id": binding.id,
        "user_id": user.id,
        "username": user.username,
        "kind": user.kind,
        "repository": binding.repository,
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
    except (github_oidc.InvalidOidcToken, agents_service.AgentNotBound,
            agents_service.AgentBindingDangling):
        # Bad signature/alg/iss/exp, missing claim, unprovisioned workload, wrong
        # audience, or a dangling binding — all collapse to one opaque 401.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or unbound token")

    # Ban gate at the mint door — the SAME _deny_if_banned enumeration every credential
    # login funnels through (#1927 anti-fragmentation): a suspended agent gets no fresh
    # token. Its live socket is dropped separately by hub.disconnect_user at ban time.
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
