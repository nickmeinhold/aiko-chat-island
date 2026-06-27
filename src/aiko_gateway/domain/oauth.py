"""Provider ID-token verification — the social sign-in security boundary (#13).

This is the load-bearing wall of federation. The app obtains an ID token from
Apple/Google ON-DEVICE and POSTs it here; this module is the ONLY place a
third-party token is trusted, and only after every grammar crossing is checked
and fails CLOSED:

  1. alg pin    — RS256 only, AND the header `alg` is pre-checked before any key
                  is fetched. Defeats alg-confusion: an attacker who flips `alg`
                  to HS256 and signs with the provider's PUBLIC key would, under
                  a permissive verifier, have that public key used as an HMAC
                  secret and pass. We never read the algorithm FROM the token.
  2. signature  — verified against the provider JWKS key matched by `kid`.
  3. aud        — must be in OUR explicit client-ID allowlist. EMPTY allowlist
                  ⇒ reject ALL (a token minted for any other Apple/Google app
                  must never authenticate here).
  4. iss        — pinned to the provider's issuer(s). Checked MANUALLY after
                  decode (PyJWT's multi-issuer handling varies by version, and
                  Google legitimately uses two iss spellings).
  5. exp/iat    — required and enforced by PyJWT.

Distinct from domain.security: that module issues+verifies OUR symmetric HS256
tokens with OUR secret. This module ONLY verifies SOMEONE ELSE'S asymmetric
RS256 tokens against their published keys. The two never share a code path — the
isolation is the alg-confusion defense by construction.

NONCE / replay: native ID-token flows support an app-generated nonce echoed in
the token; verifying it requires a server-issued nonce round-trip the app must
cooperate with (separate repo). DEFERRED — named tradeoff: a token captured
within its (short) exp window could be replayed over a compromised transport.
Mitigated by TLS + short provider exp; full mitigation needs nonce (follow-up
task). `verify_id_token` accepts `expected_nonce` so wiring it later is a no-op
to callers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import jwt

from ..config import settings


class OAuthError(Exception):
    """Base for social-verify failures."""


class UnknownProvider(OAuthError):
    """The provider slug isn't one we support."""


class InvalidProviderToken(OAuthError):
    """The token failed verification (bad alg/sig/aud/iss/exp, unknown kid).
    Maps to 401 — the caller's credential is bad."""


class ProviderUnavailable(OAuthError):
    """We could not reach the provider JWKS endpoint. Maps to 503 — a transient
    OUR-SIDE/provider outage, NOT a bad credential (a 401 would teach clients to
    retry-storm against an auth failure that isn't theirs)."""


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    sub: str
    email: str | None
    suggested_name: str | None


class _JwksCache:
    """Per-provider JWKS cache keyed by `kid`.

    Hit → return the cached key. Miss → refresh exactly ONCE under a lock, then
    look again (a concurrent caller may have refreshed while we waited). Still
    missing ⇒ the caller fails closed (unknown kid = bad token). We never loop or
    refetch per-request. A network failure during refresh raises so the caller
    can map it to 503 (outage) rather than 401 (bad token).
    """

    def __init__(self, jwks_uri: str) -> None:
        self._uri = jwks_uri
        self._keys: dict[str, object] = {}
        # asyncio.Lock created lazily on first await so the module imports without
        # a running event loop (the test suite introspects this module freely).
        self._lock: "object | None" = None

    def _get_lock(self):
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def get_key(self, kid: str):
        key = self._keys.get(kid)
        if key is not None:
            return key
        async with self._get_lock():
            # Re-check under the lock: another coroutine may have refreshed.
            key = self._keys.get(kid)
            if key is not None:
                return key
            await self._refresh()
            return self._keys.get(kid)  # may be None → caller fails closed

    async def _refresh(self) -> None:
        """Fetch the JWKS and rebuild the kid→key map. Caller holds the lock.
        Raises httpx.HTTPError on a network/HTTP failure (→ ProviderUnavailable)."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(self._uri)
            resp.raise_for_status()
            jwks = resp.json()
        new_keys: dict[str, object] = {}
        for jwk in jwks.get("keys", []):
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                new_keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
            except Exception:
                # A single malformed key must not poison the whole refresh.
                continue
        # Atomic swap: readers see either the old full map or the new full map.
        self._keys = new_keys


@dataclass(frozen=True)
class _ProviderConfig:
    slug: str
    jwks_uri: str
    issuers: frozenset[str]
    jwks: _JwksCache


_PROVIDERS: dict[str, _ProviderConfig] = {
    "apple": _ProviderConfig(
        slug="apple",
        jwks_uri="https://appleid.apple.com/auth/keys",
        issuers=frozenset({"https://appleid.apple.com"}),
        jwks=_JwksCache("https://appleid.apple.com/auth/keys"),
    ),
    "google": _ProviderConfig(
        slug="google",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        # Google legitimately uses both spellings — accept either, pin to these.
        issuers=frozenset({"https://accounts.google.com", "accounts.google.com"}),
        jwks=_JwksCache("https://www.googleapis.com/oauth2/v3/certs"),
    ),
}


def _allowed_audiences(provider: str) -> list[str]:
    if provider == "apple":
        return list(settings.apple_client_ids)
    if provider == "google":
        return list(settings.google_client_ids)
    return []


async def verify_id_token(
    provider: str, id_token: str, *, expected_nonce: str | None = None,
) -> VerifiedIdentity:
    """Verify a provider ID token and return the federated identity, or raise an
    OAuthError subclass. Every check fails CLOSED."""
    cfg = _PROVIDERS.get(provider)
    if cfg is None:
        raise UnknownProvider(provider)

    # aud allowlist FIRST, before any work: empty ⇒ reject all (fail-closed).
    audiences = _allowed_audiences(provider)
    if not audiences:
        raise InvalidProviderToken(
            f"no audience allowlist configured for {provider}")

    # (1) Pre-check the header alg BEFORE fetching any key — never trust the
    # token's self-declared algorithm. Reject anything but RS256 outright.
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError as e:
        raise InvalidProviderToken(f"malformed token header: {e}") from e
    if header.get("alg") != "RS256":
        raise InvalidProviderToken(f"unexpected alg {header.get('alg')!r}")
    kid = header.get("kid")
    if not kid:
        raise InvalidProviderToken("token header missing kid")

    # (2) Resolve the signing key. A network failure here is an OUTAGE (503),
    # NOT a bad token (401).
    try:
        key = await cfg.jwks.get_key(kid)
    except httpx.HTTPError as e:
        raise ProviderUnavailable(f"could not fetch {provider} JWKS: {e}") from e
    if key is None:
        raise InvalidProviderToken("unknown signing key (kid)")

    # (3) Verify signature + aud + exp/iat. alg is HARD-PINNED here too — the
    # belt to the header pre-check's braces.
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=audiences,  # list ⇒ PyJWT accepts if token.aud intersects
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.InvalidTokenError as e:
        raise InvalidProviderToken(str(e)) from e

    # (4) Issuer pinned manually (version-robust; supports Google's two spellings).
    if claims.get("iss") not in cfg.issuers:
        raise InvalidProviderToken(f"untrusted issuer {claims.get('iss')!r}")

    # (5) Optional nonce (deferred wiring — see module docstring).
    if expected_nonce is not None and claims.get("nonce") != expected_nonce:
        raise InvalidProviderToken("nonce mismatch")

    sub = claims.get("sub")
    if not sub:
        raise InvalidProviderToken("token missing sub")

    return VerifiedIdentity(
        provider=provider,
        sub=sub,
        email=claims.get("email"),
        # Google puts `name` in the token; Apple does NOT (the app forwards it),
        # so this is best-effort and may be None for Apple.
        suggested_name=claims.get("name"),
    )
