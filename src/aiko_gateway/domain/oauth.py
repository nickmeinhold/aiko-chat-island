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
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx
import jwt

from ..config import settings


class Provider(StrEnum):
    """The closed set of supported identity providers. A StrEnum so it validates
    at the request boundary (a bad provider is a 422, not a 400 deep in verify)
    while still comparing/​hashing as its string value everywhere downstream."""
    apple = "apple"
    google = "google"


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
    """Per-provider JWKS cache keyed by `kid`, bounded on BOTH time axes.

    A cache with no time policy fails two ways at once (cage-match PR#15, both
    HIGHs):
      - too EAGER — refreshing on every unknown-kid miss lets an attacker spam
        bogus kids and amplify into a fetch-per-request storm against the
        provider, getting us rate-limited so real logins 503.
      - too LAZY — never refreshing on a kid HIT means a rotated-out or REVOKED
        key stays trusted until process restart.

    So we bound refreshes with a FLOOR and a CEILING:
      - `min_refresh_interval` (floor): never refetch more than once per window.
        A bogus-kid storm triggers ONE refresh, then serves cached/None (fail
        closed) until the window passes — amplification is capped at ~1 fetch /
        window regardless of request rate.
      - `max_age` (ceiling): once the cached set is older than this, a hit forces
        a refresh, so a revoked/rotated key stops being trusted within the window
        rather than lingering forever.

    The floor never blocks a ceiling-driven refresh: a stale set (age ≥ max_age)
    is necessarily older than the (much smaller) floor, so the refresh is always
    permitted. The floor only bites when the cache is FRESH — exactly the
    bogus-kid-spam case.
    """

    def __init__(self, jwks_uri: str, *,
                 min_refresh_interval: float = 30.0,
                 max_age: float = 3600.0) -> None:
        self._uri = jwks_uri
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0  # monotonic time of last successful refresh (0 = never)
        self._min_refresh_interval = min_refresh_interval
        self._max_age = max_age
        # asyncio.Lock created lazily on first await so the module imports without
        # a running event loop (the test suite introspects this module freely).
        # The check-and-set has no await, so it's atomic w.r.t. other coroutines.
        self._lock: "object | None" = None

    def _get_lock(self):
        import asyncio
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= self._max_age

    def _refresh_allowed(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= self._min_refresh_interval

    async def get_key(self, kid: str):
        key = self._keys.get(kid)
        if key is not None and not self._stale():
            return key
        async with self._get_lock():
            # Re-check under the lock: another coroutine may have refreshed.
            key = self._keys.get(kid)
            if key is not None and not self._stale():
                return key
            # Refresh only if the floor permits it. If we refreshed too recently,
            # do NOT refetch — serve what we have (possibly None → fail closed).
            # This is what bounds bogus-kid amplification.
            if self._refresh_allowed():
                await self._refresh()
            return self._keys.get(kid)  # may be None → caller fails closed

    async def _refresh(self) -> None:
        """Fetch the JWKS and rebuild the kid→key map. Caller holds the lock.
        Raises httpx.HTTPError (network/HTTP) or ValueError (malformed body) —
        the caller maps both to ProviderUnavailable (503), not a bad token."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(self._uri)
            resp.raise_for_status()
            jwks = resp.json()  # ValueError on a malformed body → ProviderUnavailable
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
        self._fetched_at = time.monotonic()


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
    provider: Provider | str, id_token: str, *, expected_nonce: str | None = None,
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
    except (httpx.HTTPError, ValueError) as e:
        # Network/HTTP failure OR a malformed JWKS body (resp.json() raises
        # ValueError) — both are provider-side outages, not a bad credential.
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
