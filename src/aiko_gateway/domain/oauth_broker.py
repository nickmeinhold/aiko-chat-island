"""Server-side OAuth2 authorization-code BROKER (#21, increment 2).

This is the SECOND federation path, complementary to domain.oauth:

  * domain.oauth      — NATIVE id_token flow. The app gets an id_token on-device
                        (Apple/Google) and we verify the asymmetric signature
                        against the provider JWKS. No client secret.
  * domain.oauth_broker (THIS) — server-side AUTHORIZATION-CODE flow. The browser
                        is redirected to the provider's authorize endpoint; the
                        provider redirects back to US with a `code`; WE exchange
                        that code for an access token using a CONFIDENTIAL client
                        secret, then fetch the user profile. For providers that
                        don't mint an id_token / don't support the native SDK flow
                        (GitHub being the first).

Both paths funnel into the SAME `VerifiedIdentity` and the SAME identity->outcome
single door in rest/auth.py — so a verified GitHub user and a verified Google
user are indistinguishable downstream.

ERROR POSTURE mirrors domain.oauth exactly (and maps to the same HTTP codes):
  * BrokerUnknownProvider  -> 404/400  (slug isn't a configured broker provider)
  * BrokerInvalidExchange  -> 401      (bad/denied code, the caller's credential
                                        is bad — e.g. a replayed single-use code)
  * BrokerUnavailable      -> 503      (provider/network outage — transient, NOT
                                        a bad credential; a 401 would teach
                                        clients to retry-storm an auth failure
                                        that isn't theirs)

SECRET HYGIENE: the client_secret and the provider access_token NEVER appear in
any log line or exception message. Exceptions carry only the HTTP status / a
generic reason. This is enforced by construction — we never interpolate the
secret/token into a string that leaves this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from ..config import settings
from .oauth import VerifiedIdentity


# --- typed errors (mirror domain.oauth) ------------------------------------ #
class BrokerError(Exception):
    """Base for broker-flow failures."""


class BrokerUnknownProvider(BrokerError):
    """The slug isn't a configured broker provider. -> 404/400 (fail-closed)."""


class BrokerInvalidExchange(BrokerError):
    """The authorization-code exchange failed or was denied (bad/expired/replayed
    code, provider returned an OAuth error). -> 401 (bad credential)."""


class BrokerUnavailable(BrokerError):
    """We could not reach the provider's token/profile endpoint (network/5xx).
    -> 503 (transient outage, NOT a bad credential)."""


# A profile fetcher: given an httpx client + the provider access token, return a
# VerifiedIdentity. Raises BrokerInvalidExchange / BrokerUnavailable on failure.
ProfileFetch = Callable[[httpx.AsyncClient, str], Awaitable[VerifiedIdentity]]


def _is_rate_limited(resp: httpx.Response) -> bool:
    """True when the response is a rate-limit signal (cage-match #30, Finding 3).

    GitHub signals rate-limiting two ways: a 429, OR a 403 with the header
    ``X-RateLimit-Remaining: 0``. A plain 403 (no remaining==0 header) is a real
    authorization failure, NOT rate-limiting. Rate-limiting is an OUTAGE (503),
    not a bad credential (401)."""
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        return resp.headers.get("X-RateLimit-Remaining") == "0"
    return False


@dataclass(frozen=True)
class BrokerProvider:
    slug: str
    display_name: str
    authorize_url: str
    token_url: str
    scopes: list[str]
    supports_pkce: bool
    fetch_profile: ProfileFetch
    # Extra headers to send on the token POST (GitHub needs Accept: json so the
    # token response is JSON, not the default form-encoded body).
    token_headers: dict[str, str] = field(default_factory=dict)

    def client_id(self) -> str:
        return _provider_client_id(self.slug)

    def client_secret(self) -> str:
        return _provider_client_secret(self.slug)

    def is_configured(self) -> bool:
        """A broker provider is usable only when BOTH its id and secret are set
        (the confidential exchange needs both). A XOR half-config is rejected at
        boot in prod (config._harden_for_production), but this stays defensive so
        a dev half-config simply lists/behaves as not-configured (fail-closed)."""
        return bool(self.client_id()) and bool(self.client_secret())


# --- per-provider secret resolution ---------------------------------------- #
# Kept as a slug->settings indirection (not baked into the dataclass) so a test
# can monkeypatch settings.github_client_id and the registry reflects it live,
# mirroring how domain.oauth._allowed_audiences reads settings at call time.
def _provider_client_id(slug: str) -> str:
    if slug == "github":
        return settings.github_client_id
    return ""


def _provider_client_secret(slug: str) -> str:
    if slug == "github":
        return settings.github_client_secret
    return ""


# --- GitHub provider ------------------------------------------------------- #
async def _github_fetch_profile(
    client: httpx.AsyncClient, access_token: str,
) -> VerifiedIdentity:
    """Fetch the GitHub user profile + primary verified email and shape it into a
    VerifiedIdentity. The access_token is passed as a Bearer header and is NEVER
    logged or placed in any raised exception."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        u = await client.get("https://api.github.com/user", headers=headers)
    except httpx.HTTPError:
        # `from None` breaks the exception chain (cage-match #30, Finding 2): the
        # httpx exception's `.request` holds the Authorization header (our access
        # token); keeping it reachable via __cause__ would leak the token. We carry
        # only the generic message.
        raise BrokerUnavailable("github profile fetch failed") from None
    if u.status_code == 401:
        # The token we just minted is rejected — treat as a bad exchange, not an
        # outage. (Shouldn't happen on a fresh token, but fail closed.)
        raise BrokerInvalidExchange("github rejected the access token")
    if _is_rate_limited(u):
        # 429, or a 403 with X-RateLimit-Remaining: 0, is rate-limiting — an
        # OUTAGE, not a bad credential (cage-match #30, Finding 3). A 401 here would
        # teach clients to retry-storm an auth failure that isn't theirs.
        raise BrokerUnavailable(f"github profile rate-limited {u.status_code}")
    if u.status_code >= 500:
        raise BrokerUnavailable(f"github profile endpoint {u.status_code}")
    if u.status_code != 200:
        raise BrokerInvalidExchange(f"github profile fetch {u.status_code}")
    try:
        profile = u.json()
    except ValueError as e:
        raise BrokerUnavailable("github profile body not JSON") from e
    if not isinstance(profile, dict) or not profile.get("id"):
        raise BrokerInvalidExchange("github profile missing id")
    sub = str(profile["id"])
    suggested_name = profile.get("name") or profile.get("login")

    # Primary AND verified email. May be absent (user hid it / not granted) ->
    # email=None, which the single door handles (identity authority is the
    # (provider, sub) pair, never email).
    email: str | None = None
    try:
        e_resp = await client.get(
            "https://api.github.com/user/emails", headers=headers)
    except httpx.HTTPError:
        e_resp = None  # email is best-effort; a fetch failure is not fatal
    if e_resp is not None and e_resp.status_code == 200:
        try:
            emails = e_resp.json()
        except ValueError:
            emails = None
        if isinstance(emails, list):
            for entry in emails:
                if (isinstance(entry, dict)
                        and entry.get("primary") and entry.get("verified")):
                    candidate = entry.get("email")
                    if isinstance(candidate, str) and candidate:
                        email = candidate
                        break

    return VerifiedIdentity(
        provider="github", sub=sub, email=email, suggested_name=suggested_name)


_GITHUB = BrokerProvider(
    slug="github",
    display_name="GitHub",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    scopes=["read:user", "user:email"],
    # GitHub OAuth Apps don't support PKCE — use the confidential client_secret
    # exchange. (GitHub Apps do; OAuth Apps, which this targets, do not.)
    supports_pkce=False,
    fetch_profile=_github_fetch_profile,
    # So the token response is JSON, not the default x-www-form-urlencoded body.
    token_headers={"Accept": "application/json"},
)


# --- registry -------------------------------------------------------------- #
_REGISTRY: dict[str, BrokerProvider] = {
    _GITHUB.slug: _GITHUB,
}


def get_provider(slug: str) -> BrokerProvider:
    """Return the BrokerProvider for a CONFIGURED slug, else raise
    BrokerUnknownProvider (fail-closed: an unknown OR an unconfigured provider is
    indistinguishable from the outside — neither leaks which it was)."""
    provider = _REGISTRY.get(slug)
    if provider is None or not provider.is_configured():
        raise BrokerUnknownProvider(slug)
    return provider


def configured_providers() -> list[BrokerProvider]:
    """Every broker provider with BOTH id and secret set (listing order stable)."""
    return [p for p in _REGISTRY.values() if p.is_configured()]


# --- redirect_uri (single source) ------------------------------------------ #
def callback_redirect_uri(slug: str) -> str:
    """The redirect_uri we register with the provider AND echo at token exchange.
    Derived from gateway_base_url in ONE place so the authorize and token requests
    can never disagree (a mismatch is a hard provider error)."""
    base = settings.gateway_base_url.rstrip("/")
    return f"{base}/v1/auth/oauth/{slug}/callback"


# --- authorize URL --------------------------------------------------------- #
def build_authorize_url(
    provider: BrokerProvider, *, state: str, code_challenge: str | None = None,
) -> str:
    """Build the provider authorize URL. code_challenge/method are included ONLY
    when the provider supports PKCE (GitHub does not)."""
    params = {
        "client_id": provider.client_id(),
        "redirect_uri": callback_redirect_uri(provider.slug),
        "scope": " ".join(provider.scopes),
        "state": state,
        "response_type": "code",
    }
    if provider.supports_pkce and code_challenge is not None:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{provider.authorize_url}?{httpx.QueryParams(params)}"


# --- code -> identity exchange --------------------------------------------- #
async def exchange_code(
    provider: BrokerProvider, *, code: str, code_verifier: str | None = None,
) -> VerifiedIdentity:
    """Exchange an authorization `code` for an access token (confidential, with
    the client_secret), then fetch the user profile -> VerifiedIdentity.

    Fails CLOSED, mirroring domain.oauth:
      * a provider OAuth error / non-2xx token response / missing access_token
        -> BrokerInvalidExchange (401): the code is bad/denied/replayed.
      * a network failure or 5xx -> BrokerUnavailable (503): a transient outage.

    The client_secret and the access_token are NEVER logged or placed in any
    raised exception message."""
    data = {
        "client_id": provider.client_id(),
        "client_secret": provider.client_secret(),
        "code": code,
        "redirect_uri": callback_redirect_uri(provider.slug),
        "grant_type": "authorization_code",
    }
    if provider.supports_pkce and code_verifier is not None:
        data["code_verifier"] = code_verifier
    headers = {"Accept": "application/json", **provider.token_headers}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                provider.token_url, data=data, headers=headers)
        except httpx.HTTPError:
            # `from None` breaks the exception chain (cage-match #30, Finding 2):
            # the httpx exception's `.request` holds the POST body, which carries
            # the client_secret; keeping it reachable via __cause__ would leak the
            # secret. We carry only the generic message.
            raise BrokerUnavailable("token endpoint unreachable") from None
        if _is_rate_limited(resp):
            # 429, or a 403 with X-RateLimit-Remaining: 0, is rate-limiting — an
            # OUTAGE, not a bad credential (cage-match #30, Finding 3).
            raise BrokerUnavailable(f"token endpoint rate-limited {resp.status_code}")
        if resp.status_code >= 500:
            raise BrokerUnavailable(f"token endpoint {resp.status_code}")
        if resp.status_code != 200:
            # 4xx from the token endpoint == a bad/denied/expired code.
            raise BrokerInvalidExchange(f"token exchange {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            # A JSON parse failure here is a provider-side oddity, not a bad
            # credential -> outage. (We sent Accept: application/json.)
            raise BrokerUnavailable("token response not JSON") from e
        if not isinstance(body, dict):
            raise BrokerUnavailable("token response not an object")
        # GitHub returns 200 with {"error": ...} on a bad code -> bad credential.
        if body.get("error"):
            raise BrokerInvalidExchange("token endpoint returned an error")
        access_token = body.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise BrokerInvalidExchange("token response missing access_token")

        return await provider.fetch_profile(client, access_token)
