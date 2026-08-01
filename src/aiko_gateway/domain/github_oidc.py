"""GitHub Actions OIDC token verification — the agent-ingress security boundary.

Citizenship for the Dreaming, Handoff H1 (claude-tasks#2403). An aiko AGENT running
in GitHub Actions authenticates by presenting a short-lived GitHub Actions OIDC token
(``core.getIDToken(aud)``) — there is NO long-lived stored secret. This module is the
ONLY place such a token is trusted, and only after every grammar crossing fails CLOSED,
in the exact spirit of ``domain/oauth.py`` (the social-verify boundary):

  1. alg pin    — RS256 only, header ``alg`` pre-checked BEFORE any key is fetched.
                  Defeats alg-confusion (an HS256 token signed with the provider's
                  PUBLIC key can never be used as an HMAC secret because we never
                  read the algorithm FROM the token).
  2. signature  — verified against GitHub's OIDC JWKS, key matched by ``kid``.
  3. iss        — pinned MANUALLY to ``https://token.actions.githubusercontent.com``
                  after decode (version-robust; PyJWT multi-iss handling varies).
  4. exp/iat    — required and enforced by PyJWT.
  5. claims     — ``repository``, ``repository_id``, ``repository_owner_id``, ``ref``,
                  ``workflow_ref``, ``sub``, ``aud`` are all REQUIRED; a token missing
                  any of them is rejected (not a KeyError). ``aud`` is also TYPE-guarded
                  (str or list[str]) so a malformed-but-signed aud fails closed at 401
                  rather than 500-ing in the caller's audience match.

WHAT THIS MODULE DOES NOT DO — the audience + binding decision lives one layer up
(``agents_service``), and it MUST: the audience the token must carry is per-binding
(the operator's requested ``aud``), and the binding is keyed on the signature-verified
``repository``/``ref``/``workflow_ref`` — so we cannot know the expected aud until AFTER
we have verified the signature and read those claims. Therefore ``aud`` is deliberately
NOT verified inside ``jwt.decode`` here (``verify_aud=False``): we only REQUIRE the
claim be present and return it verbatim; ``agents_service`` looks up the binding by the
verified claims and then asserts the token's aud matches the binding's declared aud.
This two-phase ordering is safe because ``repository``/``ref``/``workflow_ref`` are
themselves signature-protected — they cannot be forged to point at a different binding.

Distinct from ``domain.oauth`` (Apple/Google ID-token verify) only in the ISSUER, the
claim set, and the two-phase aud handling; the JWKS-fetch/cache machinery is REUSED
verbatim (``oauth._JwksCache``) so the bounded-refresh hardening (floor+ceiling,
the #64 low-uptime monotonic fix) is shared, not re-derived.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import httpx
import jwt

# Reuse the battle-tested bounded JWKS cache from the social-verify boundary. It is
# parameterised purely by its jwks_uri, so a GitHub instance is a drop-in — and it
# carries the cage-matched floor/ceiling amplification bounds + the #64 never-fetched
# monotonic sentinel fix. Importing the private name keeps that logic single-sourced
# rather than forking a second copy that could drift on the next security fix.
from .oauth import _JwksCache

log = logging.getLogger(__name__)

# GitHub Actions OIDC provider constants — grounded against the live discovery doc
# (https://token.actions.githubusercontent.com/.well-known/openid-configuration:
# issuer + jwks_uri + id_token_signing_alg_values_supported=['RS256'], 2026-07-30).
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URI = "https://token.actions.githubusercontent.com/.well-known/jwks"

# One module-level cache for the single GitHub issuer (mirrors oauth._PROVIDERS'
# per-provider cache). Tests inject a public key + fresh _fetched_at to run offline.
_jwks = _JwksCache(GITHUB_JWKS_URI)


def _fingerprint(value: object, n: int = 12) -> str:
    """A non-reversible, length-safe fingerprint for tracing (mirrors oauth._fingerprint).
    ``sub`` / ``repository`` are not secrets, but a short prefix could disclose a
    private-repo slug whole; a sha256 prefix correlates the same workload across log
    lines without logging the value."""
    if value is None:
        return "?"
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:n]


class GitHubOidcError(Exception):
    """Base for GitHub OIDC verification failures."""


class InvalidOidcToken(GitHubOidcError):
    """The token failed verification (bad alg/sig/iss/exp, unknown kid, missing
    required claim). Maps to 401 — the caller's credential is bad."""


class OidcProviderUnavailable(GitHubOidcError):
    """Could not reach GitHub's JWKS endpoint. Maps to 503 — a transient OUR-SIDE /
    GitHub outage, NOT a bad credential (a 401 would teach clients to retry-storm an
    auth failure that isn't theirs)."""


@dataclass(frozen=True)
class GitHubOidcClaims:
    """The signature-verified subset the agent door acts on. ``aud`` is the RAW claim
    (a str, or a list per the JWT spec) — NOT yet checked against any binding; the
    caller does that. ``sub`` is GitHub's ``repo:owner/repo:...`` subject, carried for
    tracing/audit only. ``repository_id`` / ``repository_owner_id`` are GitHub's
    IMMUTABLE numeric ids (emitted as strings) — the caller authorizes on THESE, not on
    the mutable ``repository`` NAME, so a recycled/transferred slug cannot mint an old
    identity."""
    repository: str
    ref: str
    workflow_ref: str
    sub: str
    repository_id: str
    repository_owner_id: str
    aud: str | list[str]


async def verify_github_oidc_token(token: str) -> GitHubOidcClaims:
    """Verify a GitHub Actions OIDC token's SIGNATURE, issuer, expiry, and required
    claims, and return them. Does NOT verify ``aud`` (see the module docstring) — the
    caller checks it against the matched binding. Every failure raises fail-closed:
    ``InvalidOidcToken`` (bad credential → 401) or ``OidcProviderUnavailable``
    (GitHub/JWKS outage → 503)."""
    # (1) Pre-check the header alg BEFORE fetching any key — never trust the token's
    # self-declared algorithm. RS256 only.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        log.warning("agent.oidc: REJECT malformed token header: %s", e)
        raise InvalidOidcToken(f"malformed token header: {e}") from e
    if header.get("alg") != "RS256":
        log.warning("agent.oidc: REJECT unexpected alg %r", header.get("alg"))
        raise InvalidOidcToken(f"unexpected alg {header.get('alg')!r}")
    kid = header.get("kid")
    if not kid:
        log.warning("agent.oidc: REJECT token header missing kid")
        raise InvalidOidcToken("token header missing kid")

    # (2) Resolve the signing key. A network failure here is an OUTAGE (503).
    try:
        key = await _jwks.get_key(kid)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("agent.oidc: OUTAGE could not fetch JWKS: %s", e)
        raise OidcProviderUnavailable(f"could not fetch GitHub JWKS: {e}") from e
    if key is None:
        log.warning("agent.oidc: REJECT unknown signing key kid=%s", _fingerprint(kid))
        raise InvalidOidcToken("unknown signing key (kid)")

    # (3) Verify signature + exp/iat + require the claims we bind on. alg HARD-PINNED
    # here too (belt to the header pre-check's braces). aud is REQUIRED-present but
    # NOT matched here — the caller checks it against the binding's declared aud.
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "require": ["exp", "iat", "sub", "aud", "iss",
                            "repository", "repository_id", "repository_owner_id",
                            "ref", "workflow_ref"],
                "verify_aud": False,
            },
        )
    except jwt.InvalidTokenError as e:
        log.warning("agent.oidc: REJECT jwt.decode failed: %s", e)
        raise InvalidOidcToken(str(e)) from e

    # (4) Issuer pinned manually (version-robust).
    if claims.get("iss") != GITHUB_ISSUER:
        log.warning("agent.oidc: REJECT untrusted issuer %r", claims.get("iss"))
        raise InvalidOidcToken(f"untrusted issuer {claims.get('iss')!r}")

    # (5) The required claims are guaranteed present by the `require` option above, but
    # re-read defensively as the right types (a non-string repo/ref/workflow_ref is a
    # malformed token, not a server error).
    repository = claims.get("repository")
    ref = claims.get("ref")
    workflow_ref = claims.get("workflow_ref")
    sub = claims.get("sub")
    repository_id = claims.get("repository_id")
    repository_owner_id = claims.get("repository_owner_id")
    aud = claims.get("aud")
    if not (isinstance(repository, str) and isinstance(ref, str)
            and isinstance(workflow_ref, str) and isinstance(sub, str)
            and isinstance(repository_id, str) and isinstance(repository_owner_id, str)):
        log.warning("agent.oidc: REJECT non-string identity claim")
        raise InvalidOidcToken("malformed identity claims")

    # aud TYPE-GUARD: the caller does `list(token_aud)`/per-candidate compare, so a
    # signed token with a non-str/non-list-of-str aud (e.g. a number or object) would
    # crash there with a 500. Reject it fail-closed as a malformed token (401) HERE,
    # at the verify boundary, rather than letting a malformed-but-signed shape reach
    # the audience match. (`verify_aud=False` above only skips MATCHING aud, not
    # validating its TYPE.)
    if not (isinstance(aud, str)
            or (isinstance(aud, list) and all(isinstance(a, str) for a in aud))):
        log.warning("agent.oidc: REJECT malformed aud claim type %s", type(aud).__name__)
        raise InvalidOidcToken("malformed aud claim")

    log.info("agent.oidc: OK repository=%s workflow_ref=%s sub=%s",
             _fingerprint(repository), _fingerprint(workflow_ref), _fingerprint(sub))
    return GitHubOidcClaims(
        repository=repository, ref=ref, workflow_ref=workflow_ref, sub=sub,
        repository_id=repository_id, repository_owner_id=repository_owner_id, aud=aud)
