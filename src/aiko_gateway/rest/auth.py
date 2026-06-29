"""Auth endpoints: register (gated), login, refresh, me.

/register is open in dev for testing and closed by default in production
(settings.open_registration, resolved by environment). With open registration a
self-created account can read everything until I2 membership lands (#36), so
prod fails closed; an explicit OPEN_REGISTRATION override re-opens it.
"""
from __future__ import annotations

import datetime as dt
import logging

import jwt
from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse, InvalidRegistrationResponse,
)

from ..config import settings
from ..domain import (
    accounts_service, handoff_service, nonce_service, oauth, oauth_broker,
    passkey_service, security, state_service, users_service,
)
from ..domain.models import PasskeyOperation, User
from ..domain.oauth import Provider, VerifiedIdentity
from ..domain.pkce import (
    is_valid_app_challenge, make_pkce_pair, verify_app_challenge,
)
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1/auth", tags=["auth"])

log = logging.getLogger(__name__)


class RegisterReq(BaseModel):
    username: str
    display_name: str = ""
    password: str


class LoginReq(BaseModel):
    username: str
    password: str


class RefreshReq(BaseModel):
    refresh_token: str


def _user_view(u: User) -> dict:
    return {"user_id": u.id, "username": u.username,
            "display_name": u.display_name, "aiko_username": u.aiko_username}


def _tokens(user_id: str) -> dict:
    return {"access_token": security.issue_access(user_id),
            "refresh_token": security.issue_refresh(user_id)}


@router.post("/register")
async def register(req: RegisterReq, session: DbSession) -> dict:
    if not settings.open_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed")
    try:
        user = await users_service.create_user(
            session, username=req.username,
            display_name=req.display_name, password=req.password,
        )
    except IntegrityError:
        # Roll back the failed transaction before reusing/closing the session
        # (a failed commit leaves it needing rollback).
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken")
    return {**_tokens(user.id), "user": _user_view(user)}


@router.post("/login")
async def login(req: LoginReq, session: DbSession) -> dict:
    user = await users_service.authenticate(session, req.username, req.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return {**_tokens(user.id), "user": _user_view(user)}


class SocialReq(BaseModel):
    provider: Provider     # closed set → a bad value is a 422 at the boundary
    id_token: str          # the provider ID token obtained on-device
    # Replay defense (#13): the RAW app-generated nonce that was fed (hashed, for
    # Apple) into the provider sign-in request and is echoed in the id_token. The
    # gateway applies the provider-specific transform; the app sends it raw.
    # Optional on the wire today (the live app omits it); becomes mandatory once
    # settings.social_nonce_required is flipped on after the app ships nonces.
    # min_length=1: a BLANK nonce is malformed, not "supplied" — reject it at the
    # boundary (422) so an empty string can never become a downgrade channel that
    # slips past presence-enforcement (Carnot, cage-match PR#32). None = absent.
    # min_length=1 rejects a blank nonce at the boundary; max_length=64 caps it to
    # the issued size (token_urlsafe(32) -> 43 chars, stored String(64)) so an
    # oversized attacker string never reaches the DB comparison (cage-match PR#33).
    nonce: str | None = Field(default=None, min_length=1, max_length=64)


class SocialClaimReq(BaseModel):
    provisioning_token: str
    # Bounded at the public front door (cage-match PR#15): a handle becomes the
    # username + aiko_username (both String(64)); reject empty/whitespace/overlong
    # here with a 422 rather than deferring to DB behaviour and 409/500 ambiguity.
    handle: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=128)

    @field_validator("handle", "display_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("handle")
    @classmethod
    def _handle_nonempty_after_strip(cls, v: str) -> str:
        if not v:
            raise ValueError("handle must not be blank")
        return v


async def _resolve_identity(
    session: AsyncSession, identity: VerifiedIdentity,
) -> dict:
    """THE SINGLE DOOR (#21): turn a VERIFIED federated identity into a session
    outcome. There is exactly ONE place that does this — both the native
    /social path and the broker /callback path call it, so a verified identity
    behaves identically whichever flow produced it.

      * KNOWN (provider, sub) → authenticated dict: real access+refresh tokens +
        the user view.
      * NEW identity → provisioning dict: a short-lived signed provisioning token
        carrying (provider, sub, suggested_name, email) + the suggested fields.
        No DB row until the user claims a handle at /social/claim.

    The caller is responsible for having VERIFIED the identity (id-token
    signature or authorization-code exchange) before calling — this door trusts
    its `identity` argument absolutely."""
    user = await users_service.get_user_by_social(
        session, identity.provider, identity.sub)
    if user is not None:
        return {**_tokens(user.id), "user": _user_view(user)}

    provisioning_token = security.issue_provisioning(
        identity.provider, identity.sub,
        suggested_name=identity.suggested_name, email=identity.email,
    )
    return {
        "status": "provisioning",
        "provisioning_token": provisioning_token,
        "suggested_name": identity.suggested_name,
        "email": identity.email,
    }


class NonceResp(BaseModel):
    nonce: str


@router.post("/nonce")
async def issue_nonce(session: DbSession) -> NonceResp:
    """Mint a server-ISSUED single-use nonce for the native social sign-in flow
    (#13 option (a)). The app calls this FIRST, feeds the nonce (hashed, for Apple)
    into the Sign-in-with-Apple/Google request, then echoes it to /social with the
    id_token, where the gateway redeems it exactly once — so a captured /social
    request can't be replayed (the nonce is already burned). Pre-auth (the user
    isn't signed in yet), gated only by the social-sign-in kill switch.

    NAMED TRADEOFF: issuance is unauthenticated and unthrottled. Nonces are tiny
    and self-expire on a short TTL (same posture as the broker /start), so a flood
    is bounded by the TTL; a sweeper / rate-limit is a follow-up if the table grows.
    """
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    return NonceResp(nonce=await nonce_service.issue_nonce(session))


@router.post("/social")
async def social(req: SocialReq, session: DbSession) -> dict:
    """Verify a provider ID token. Known identity → real tokens. Brand-new
    identity → a short-lived provisioning token to carry the verified identity
    into /social/claim (no DB row until the user picks a handle)."""
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    # Presence enforcement (#13): refuse a nonce-less request only when policy
    # requires it. Verification of a SUPPLIED nonce happens unconditionally inside
    # verify_id_token — this gate only governs whether MISSING is tolerated, so the
    # breaking flip is one config flag once the app ships nonce generation.
    if settings.social_nonce_required and not req.nonce:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "nonce required")
    try:
        identity = await oauth.verify_id_token(
            req.provider, req.id_token, expected_nonce=req.nonce)
    except oauth.UnknownProvider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown provider")
    except oauth.ProviderUnavailable:
        # Provider/JWKS outage — transient, not a bad credential. 503 so clients
        # back off rather than treating it as an auth failure (401) and retrying.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "provider temporarily unavailable")
    except oauth.InvalidProviderToken:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid provider token")

    # Server-issuance + single-use (#13 option (a)): a supplied nonce must be one
    # the gateway ISSUED (POST /v1/auth/nonce) and not yet redeemed. Consumed AFTER
    # the token verifies — so a transient JWKS 503 or a bad token does NOT burn the
    # nonce (the user retries with the same one; cage-match PR#33, Carnot MEDIUM) —
    # but BEFORE any session is issued. Replay still collapses here: a replayed
    # valid token verifies, then the already-burned nonce fails this consume; the
    # atomic guard also arbitrates concurrent replays to a single winner. With the
    # provider-claim match (inside verify above) both properties hold: the nonce is
    # server-issued + single-use AND provider-bound to THIS token.
    #
    # ATOMIC-WITH-OUTCOME (#24): consume_nonce CLAIMS the nonce (conditional UPDATE,
    # row locked) but does NOT commit. Concurrent replays collapse to at most one
    # COMMITTED winner — a claim whose request then fails rolls back and correctly
    # leaves the nonce usable for the retry (cage-match #35, Carnot: the guarantee
    # is one committed outcome per nonce, not one attempted request). We commit
    # only AFTER _resolve_identity succeeds, so a transient failure in the OUTCOME
    # (a DB-read outage during the user lookup) rolls back the claim via the
    # request session and the app retries the SAME nonce — no stranded sign-in.
    # Deferring the commit is safe here ONLY because there is no network IO between
    # the claim and the commit (just a local read + JWT mint), so the SQLite write
    # lock is held momentarily. The broker /callback is DELIBERATELY not symmetric:
    # it has the provider code-exchange in that gap (holding the lock across a
    # network call would stall every gateway write) AND it is a one-shot browser
    # redirect with no retry channel, so atomicity there is all cost and no benefit
    # — it commits its state burn eagerly (see oauth_callback / consume_state).
    if req.nonce is not None and not await nonce_service.consume_nonce(
            session, req.nonce):
        await session.rollback()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired nonce")
    # Single door: verified identity → session/provisioning outcome.
    outcome = await _resolve_identity(session, identity)
    # Commit AFTER the outcome succeeds — this makes the nonce burn durable, atomic
    # with the sign-in. UNCONDITIONAL (not gated on req.nonce) so the commit
    # boundary holds even if _resolve_identity ever grows a write path, and so the
    # optional-nonce mode can't hide it; when there is no nonce and nothing pending
    # it is a harmless no-op (cage-match #35, Carnot + Maxwell).
    await session.commit()
    return outcome


@router.post("/social/claim")
async def social_claim(req: SocialClaimReq, session: DbSession) -> dict:
    """Complete provisioning: verify the provisioning token (OUR token, so the
    identity it carries cannot be forged), create the user atomically, and return
    real tokens. Serves BOTH the social flow (creates a SocialIdentity) and the
    passkey flow (#1471 — creates a PasskeyCredential from the verified material the
    token carries); the token's shape selects the path. ONE claim endpoint, as the
    app contract requires."""
    try:
        pending = security.decode_provisioning(req.provisioning_token)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired provisioning token")
    if pending.get("passkey_credential") is not None:
        # Passkey claim: ungated like the passkey endpoints (deploy-dark). Atomic +
        # replay-safe via the credential_id UNIQUE constraint (see create_passkey_user).
        try:
            user = await users_service.create_passkey_user(
                session, handle=req.handle, display_name=req.display_name,
                email=pending["email"], material=pending["passkey_credential"],
            )
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "handle already taken or passkey already claimed")
        return {**_tokens(user.id), "user": _user_view(user)}
    # Social claim — gated on social sign-in being enabled (unchanged).
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    try:
        user = await users_service.create_social_user(
            session,
            provider=pending["provider"], provider_sub=pending["provider_sub"],
            handle=req.handle, display_name=req.display_name,
            email=pending["email"],
        )
    except IntegrityError:
        # Handle already taken OR this identity was already claimed (race).
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "handle already taken or identity already claimed")
    return {**_tokens(user.id), "user": _user_view(user)}


# --- WebAuthn passkeys (#1471) --------------------------------------------- #
# Passwordless credential sign-in. `start` mints a single-use challenge; `finish`
# verifies the device's attestation/assertion (py_webauthn) and routes into the
# SAME outcome shape as /social + the broker (the app's one _resolveOutcome door).
# Endpoints are UNGATED (deploy-dark) — only the /providers advertisement is gated
# on settings.passkey_enabled, so the flow can be exercised on a real device before
# the app surfaces the buttons (the handoff's "deploy endpoints, advertise last").

class PasskeyFinishReq(BaseModel):
    state: str
    credential: dict


@router.post("/passkey/register/start")
async def passkey_register_start(session: DbSession) -> dict:
    """Begin registration for an ANONYMOUS caller (first-passkey-creates-account).
    Returns {state, options} — the raw WebAuthn-JSON the platform authenticator
    parses. No body, no prior session."""
    return await passkey_service.start_registration(session)


@router.post("/passkey/register/finish")
async def passkey_register_finish(req: PasskeyFinishReq, session: DbSession) -> dict:
    """Verify the attestation and return a PROVISIONING outcome (new identity must
    claim a handle). The verified credential rides in the provisioning token and is
    persisted at /social/claim — byte-identical shape to the social provisioning
    outcome so the app's single resolver accepts it."""
    raw = await passkey_service.consume_challenge(
        session, req.state, PasskeyOperation.REGISTER)
    if raw is None:
        await session.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "invalid or expired passkey challenge")
    try:
        material = passkey_service.verify_registration(
            raw_challenge=raw, credential=req.credential)
    except InvalidRegistrationResponse:
        # Covers both a malformed credential and a failed attestation (py_webauthn
        # wraps parse failures too). Fail closed; the deferred challenge burn rolls
        # back so an honest retry of the SAME ceremony is possible.
        await session.rollback()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "passkey registration verification failed")
    token = security.issue_provisioning(
        "passkey", material["credential_id"], passkey_credential=material)
    await session.commit()  # burn the challenge durably — the ceremony completed
    return {
        "status": "provisioning",
        "provisioning_token": token,
        "suggested_name": None,
        "email": None,
    }


@router.post("/passkey/authenticate/start")
async def passkey_authenticate_start(session: DbSession) -> dict:
    """Begin usernameless/discoverable authentication (empty allowCredentials).
    Returns {state, options}."""
    return await passkey_service.start_authentication(session)


@router.post("/passkey/authenticate/finish")
async def passkey_authenticate_finish(
    req: PasskeyFinishReq, session: DbSession,
) -> dict:
    """Verify the assertion against the STORED public key and issue a session
    (authenticated outcome — a known credential is never provisioning)."""
    raw = await passkey_service.consume_challenge(
        session, req.state, PasskeyOperation.AUTHENTICATE)
    if raw is None:
        await session.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "invalid or expired passkey challenge")
    try:
        cred_id = passkey_service.credential_id_of(req.credential)
    except (KeyError, TypeError, ValueError):
        await session.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "malformed passkey credential")
    cred = await passkey_service.get_credential(session, cred_id)
    if cred is None:
        await session.rollback()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "unknown passkey credential")
    try:
        new_count = passkey_service.verify_authentication(
            raw_challenge=raw, credential=req.credential,
            public_key_b64=cred.public_key, current_sign_count=cred.sign_count)
    except InvalidAuthenticationResponse:
        await session.rollback()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "passkey authentication verification failed")
    user = await users_service.get_by_id(session, cred.user_id)
    if user is None:  # defensive — the FK guarantees it; fail closed regardless
        await session.rollback()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "unknown passkey credential")
    cred.sign_count = new_count
    cred.last_used_at = dt.datetime.now(dt.timezone.utc)
    outcome = {**_tokens(user.id), "user": _user_view(user)}
    # Durable AFTER the outcome: the challenge burn + sign_count bump commit atomic
    # with the sign-in (a downstream failure rolls both back — the #24 contract).
    await session.commit()
    return outcome


@router.post("/refresh")
async def refresh(req: RefreshReq) -> dict:
    try:
        user_id = security.decode_token(req.refresh_token, expected_type="refresh")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    return {"access_token": security.issue_access(user_id)}


# --- OAuth broker (server-side authorization-code flow, #21) ---------------- #
# A SEPARATE concern from the native /social path but the SAME prefix and the
# SAME single door (_resolve_identity). The browser drives /start -> provider ->
# /callback; the app then redeems the handoff via /exchange. Tokens are NEVER put
# in a redirect URL and NEVER stored at rest — minted only at /exchange time.

def _app_callback_redirect(*, code: str | None = None, error: str | None = None
                           ) -> RedirectResponse:
    """Build the 302 back to the APP'S callback. The target is settings
    .app_oauth_callback_url — a FIXED config value, NEVER read from any request
    parameter (open-redirect defense). Only `code` (a handoff code) or `error`
    (a coarse, non-sensitive indicator) is appended."""
    base = settings.app_oauth_callback_url
    sep = "&" if "?" in base else "?"
    if code is not None:
        return RedirectResponse(f"{base}{sep}code={code}", status_code=302)
    # A coarse, non-sensitive error class only — never the provider's raw error
    # string (which could carry attacker-influenced content into the browser).
    return RedirectResponse(f"{base}{sep}error={error or 'oauth_failed'}",
                            status_code=302)


def _provider_or_404(slug: str) -> oauth_broker.BrokerProvider:
    try:
        return oauth_broker.get_provider(slug)
    except oauth_broker.BrokerUnknownProvider:
        # An unknown OR unconfigured provider — fail-closed, 404 (don't leak which).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: str, session: DbSession, app_challenge: str | None = None,
) -> RedirectResponse:
    """Begin the broker flow: mint a SERVER-SIDE single-use state nonce (storing
    the PKCE code_verifier server-side when supported), and 302 to the provider's
    authorize URL with ONLY the opaque nonce as `state`. 404 if the provider isn't
    a configured broker provider.

    The `state` sent to the provider is the opaque nonce — NOT a signed token —
    so it carries no secret. For PKCE providers the code_verifier stays in the
    state row and ONLY the code_challenge crosses the wire; the verifier never
    leaves the server (cage-match #30, Finding 1).

    `app_challenge` is the APP's S256 challenge base64url(sha256(app_verifier))
    (cage-match #37). REQUIRED and fail-closed: it binds the eventual handoff to
    the originating app so a handoff code intercepted via a hijacked custom scheme
    cannot be redeemed without the app-held verifier. The only legitimate caller
    is the app, which always sends it; a missing/short value is a misuse → 400."""
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    prov = _provider_or_404(provider)
    # The app_challenge MUST be the exact shape of an S256 base64url digest (43
    # url-safe chars). Validate at ingress (cage-match #37 r2, Carnot LOW) so a
    # malformed / wrong-length / non-ASCII / non-b64url value is rejected here as a
    # 400 rather than stored to fail as a later 401 (and so the store only ever
    # holds well-formed challenges).
    if not app_challenge or not is_valid_app_challenge(app_challenge):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "missing or malformed app_challenge")
    code_verifier = code_challenge = None
    if prov.supports_pkce:
        code_verifier, code_challenge = make_pkce_pair()
    nonce = await state_service.create_state(
        session, provider=prov.slug, code_verifier=code_verifier,
        app_challenge=app_challenge)
    url = oauth_broker.build_authorize_url(
        prov, state=nonce, code_challenge=code_challenge)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str, session: DbSession,
    code: str | None = None, state: str | None = None, error: str | None = None,
) -> RedirectResponse:
    """The provider redirects here. ANY failure → 302 to the app callback with an
    error indicator (never a raw 500 to a browser). Success → store a minimal
    handoff payload and 302 to the app callback with ?code=<handoff_code>.

    NOTE on open-redirect: the redirect target is ALWAYS
    settings.app_oauth_callback_url (a fixed config value). No request parameter
    — not `state`, not anything — influences WHERE we redirect; params only carry
    the handoff code or an error class."""
    # Kill-switch: when social sign-in is administratively disabled the broker is
    # off too (uniform with the native /social gate). A disabled-flag callback is
    # not a normal user path, so a plain 403 is fine here (NOT a redirect).
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    # The provider/path is validated first so even an error-callback for an
    # unknown provider 404s rather than redirecting (a probe shouldn't bounce).
    prov = _provider_or_404(provider)

    # User denied / provider-side error → graceful redirect, not a 500.
    if error:
        return _app_callback_redirect(error="provider_denied")
    if not code or not state:
        return _app_callback_redirect(error="missing_code")

    # Consume the state nonce ATOMICALLY (single-use): the same nonce can never be
    # redeemed twice, so a captured callback URL can't be replayed at this layer
    # (cage-match #30, Finding 1). None = missing / expired / already-consumed /
    # forged → bad_state. The provider stored in the row MUST match the path
    # provider (a nonce minted for provider A must not be used on provider B's
    # callback).
    # At-most-once (cage-match #30 r2, DELIBERATE — not an atomicity bug): state is
    # consumed before the external exchange (security: never exchange on an
    # unvalidated state); a transient failure AFTER consume burns this one nonce —
    # the user simply re-initiates /start for a fresh nonce. This is intentional
    # at-most-once, not an atomicity bug. Service-commit convention (get_session
    # does not commit); a single request txn would hold the state row-lock across
    # the provider network call.
    #
    # NOT atomic-with-outcome, DELIBERATELY asymmetric to /social (#24). Two
    # channel-rooted reasons make eager consume correct HERE where it was a bug
    # there: (1) deferring the commit would hold the SQLite write lock across the
    # exchange_code network call below, stalling every other gateway writer up to
    # busy_timeout; (2) this is a ONE-SHOT browser redirect — on any failure we 302
    # to the app with ?error and the user restarts from /start with a fresh state,
    # so an un-burned state has no retry channel to be reused on and atomicity buys
    # nothing observable. /social is a retryable API call, so deferring its commit
    # is a real win; here it is all cost and no benefit.
    st = await state_service.consume_state(session, state)
    if st is None:
        return _app_callback_redirect(error="bad_state")
    if st["provider"] != prov.slug:
        return _app_callback_redirect(error="state_provider_mismatch")
    # Fail CLOSED on a state with no app_challenge (cage-match #37 r2, Carnot
    # MEDIUM). consume_state returns provider/code_verifier but the binding lives in
    # the row's app_challenge; a missing one would mint a handoff redeemable WITHOUT
    # a verifier (the verifierless legacy path at /exchange). /start now requires a
    # valid challenge, so the only way to reach here challenge-less is a pre-binding
    # in-flight state straddling the deploy — and GitHub sign-in is not live yet, so
    # there are none to protect. Refusing here makes the binding invariant ABSOLUTE:
    # no verifierless handoff is ever created.
    if not st.get("app_challenge"):
        return _app_callback_redirect(error="bad_state")

    # Exchange the code for a verified identity. Both failure classes resolve to a
    # graceful redirect-with-error for the BROWSER (never a raw status leak). The
    # PKCE code_verifier comes from the SERVER-SIDE state row — it never crossed
    # the wire.
    try:
        identity = await oauth_broker.exchange_code(
            prov, code=code, code_verifier=st.get("code_verifier"))
    except oauth_broker.BrokerInvalidExchange:
        return _app_callback_redirect(error="exchange_failed")
    except oauth_broker.BrokerUnavailable:
        return _app_callback_redirect(error="provider_unavailable")
    except oauth_broker.BrokerError:
        return _app_callback_redirect(error="oauth_failed")

    # Single door → known/new outcome. Reduce to a MINIMAL handoff payload (NEVER
    # minted tokens — those are minted at /exchange time).
    outcome = await _resolve_identity(session, identity)
    if "access_token" in outcome:
        # Known user: the outcome carries a freshly-minted pair, but we store ONLY
        # the user_id and re-mint at exchange (tokens never live at rest).
        payload = {"kind": "authenticated", "user_id": outcome["user"]["user_id"]}
    else:
        payload = {
            "kind": "provisioning",
            "provider": identity.provider,
            "provider_sub": identity.sub,
            "suggested_name": identity.suggested_name,
            "email": identity.email,
        }
    # Bind the handoff to the originating app (cage-match #37): carry the app's
    # S256 challenge from the state row so /exchange can require the matching
    # verifier. A handoff intercepted via a hijacked custom scheme is then
    # unredeemable without the verifier (which never left the app).
    payload["app_challenge"] = st.get("app_challenge")
    handoff_code = await handoff_service.create_handoff(session, payload)
    return _app_callback_redirect(code=handoff_code)


class OAuthExchangeReq(BaseModel):
    code: str = Field(min_length=1)
    # The app-held verifier whose S256 hash must match the handoff's app_challenge
    # (cage-match #37). Optional in the type so a legacy/no-binding handoff still
    # parses, but ENFORCED below whenever the handoff carries a challenge.
    # max_length bounds the input that gets SHA-256'd at the boundary (cage-match
    # #37 r2): a legit verifier is ~43 chars (b64url of 32 bytes); 128 is RFC 7636's
    # PKCE verifier ceiling. Caps the hash-input DoS surface to a constant.
    app_verifier: str | None = Field(default=None, max_length=128)


@router.post("/oauth/exchange")
async def oauth_exchange(req: OAuthExchangeReq, session: DbSession) -> dict:
    """Redeem a single-use handoff code for the final session JSON. Missing /
    expired / already-consumed → 401. The redemption is atomic (single-use, the
    double-spend guard lives in handoff_service.consume_handoff)."""
    if not settings.social_signin_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "social sign-in is disabled")
    payload = await handoff_service.consume_handoff(session, req.code)
    if payload is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
    # App-binding gate (cage-match #37): the handoff is bound to an app challenge
    # (every handoff is — /callback now fails closed without one), and the caller
    # MUST present the matching verifier. A handoff code intercepted via a hijacked
    # Android custom scheme is unredeemable here — the thief never had the verifier.
    # The handoff is already consumed above (single-use), so a failed verify also
    # burns the code, foreclosing a second guess. Same opaque 401 — never leak which
    # check failed.
    #
    # SCOPE / known limitation (cage-match #37 r2, Carnot HIGH): this closes PASSIVE
    # interception of a legitimately-app-started flow. It does NOT prove the handoff
    # belongs to a trusted app — a MALICIOUS app that runs its OWN /start (its own
    # challenge+verifier) and drives the user through the provider can redeem its own
    # handoff. That is the app-AUTHENTICITY problem (the OS letting two apps claim
    # aikochat://), addressed by verified app links / universal links / platform
    # attestation — NOT by handoff binding, and not regressed by this PR. Tracked
    # separately. The `if challenge:` guard stays as defense-in-depth even though
    # /callback no longer mints a challenge-less handoff.
    challenge = payload.get("app_challenge")
    if challenge:
        if not req.app_verifier or not verify_app_challenge(
                req.app_verifier, challenge):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
    # Kind-guard fails CLOSED (cage-match #30, Finding 4): a payload whose `kind`
    # isn't one we wrote is treated as invalid (401), never falling through to a
    # KeyError/500. We only ever write "authenticated"/"provisioning" server-side,
    # so anything else is a corrupted/unexpected row.
    kind = payload.get("kind")
    if kind not in {"authenticated", "provisioning"}:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
    # Complete the kind-guard (cage-match #30 r2): validate the REQUIRED fields per
    # kind BEFORE indexing, failing closed with 401 (not a KeyError/500) if absent.
    # We only ever write complete payloads server-side, so a missing field is a
    # corrupted/unexpected row — treat it like any other invalid handoff.
    if kind == "authenticated":
        if not payload.get("user_id"):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
    else:  # provisioning
        if not payload.get("provider") or not payload.get("provider_sub"):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
    if kind == "authenticated":
        user_id = payload["user_id"]
        user = await users_service.get_by_id(session, user_id)
        if user is None:
            # The user vanished between callback and exchange (deleted account).
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or expired handoff code")
        return {**_tokens(user.id), "user": _user_view(user)}
    # provisioning — mint the provisioning token now (the verified identity was
    # carried in the handoff payload; it cannot be forged because the payload was
    # written server-side under an unguessable code).
    provisioning_token = security.issue_provisioning(
        payload["provider"], payload["provider_sub"],
        suggested_name=payload.get("suggested_name"), email=payload.get("email"),
    )
    return {
        "status": "provisioning",
        "provisioning_token": provisioning_token,
        "suggested_name": payload.get("suggested_name"),
        "email": payload.get("email"),
    }


@router.get("/providers")
async def list_providers() -> dict:
    """List the sign-in providers the client may offer.

    Native providers (apple/google — kind:"native") are included when their
    client_ids are configured (the native flow needs no client secret). Broker
    providers (kind:"broker") are the configured server-side ones (both id +
    secret set). Fail-closed: an unconfigured provider is simply absent.

    When social sign-in is administratively disabled, the social providers show
    nothing (consistent with the native + broker gates returning 403 in that
    state). Passkey (#1471) is an INDEPENDENT feature gated on its own
    passkey_enabled flag — flipped on LAST, after the endpoints + .well-known are
    live, so it can deploy dark."""
    providers: list[dict] = []
    if settings.passkey_enabled:
        providers.append(
            {"slug": "passkey", "display_name": "Passkey", "kind": "passkey"})
    if settings.social_signin_enabled:
        if settings.apple_client_ids:
            providers.append(
                {"slug": "apple", "display_name": "Apple", "kind": "native"})
        if settings.google_client_ids:
            providers.append(
                {"slug": "google", "display_name": "Google", "kind": "native"})
        for p in oauth_broker.configured_providers():
            providers.append(
                {"slug": p.slug, "display_name": p.display_name, "kind": "broker"})
    return {"providers": providers}


me_router = APIRouter(prefix="/v1", tags=["auth"])


@me_router.get("/me")
async def me(user: CurrentUser) -> dict:
    return _user_view(user)


@me_router.delete("/account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(user: CurrentUser, session: DbSession) -> Response:
    """Permanently delete the authenticated user's account (Apple 5.1.1(v)).

    Hard-deletes the user row + federated identities + channel memberships and
    anonymizes the user's authored messages (the conversation survives, the
    account link does not). 409 if the user is the sole admin of any channel —
    they must hand those over or leave them first."""
    try:
        await accounts_service.delete_user_account(session, user.id)
    except accounts_service.CannotDeleteSoleAdmin as e:
        # No rollback: the guard raises before delete_user_account performs ANY
        # write (only SELECTs precede it), so there is nothing to undo. (Carnot
        # suggested a defensive rollback here; rejected — on the shared async test
        # session it raises MissingGreenlet, and in prod it is a no-op on a fresh
        # per-request session. The guard-before-writes invariant is what keeps this
        # safe; if future guard code writes before raising, roll back THEN.)
        # The channel ids are ULIDs — useless in a user-facing string — so log them
        # server-side and return a generic, actionable message (cage-match, Carnot).
        log.info("account deletion blocked: user=%s sole admin of channels=%s",
                 user.id, e.channel_ids)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You are the sole admin of one or more channels. Transfer them to "
            "another member or leave them before deleting your account.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
