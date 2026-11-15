"""Session revocation via per-user token_generation (#1914).

The LIFT CONDITION for Design 05 social recovery: bumping a user's
`token_generation` must invalidate EVERY previously-issued JWT (access + refresh)
at EVERY authentication ingress, while a token minted at the current generation
still works. Stateless HS256 tokens have no revocation on their own; the `gen`
claim + a live equality check against the DB row is the revocation mechanism.

These are the ingress-completeness twin of the ban tests
(test_moderation_actions): a ban rejects a suspended ROW, token_generation
rejects a stale SESSION. Both must gate all three ingresses — REST
get_current_user, token refresh, and the WS handshake — or the revocation leaks
(the auth-ingress fragmentation risk, #1927). The WS handshake in particular is
the ingress the task's prose fix-list omitted; it authenticates outside
get_current_user via decode_token directly, so it gets its own test here.

Migration safety is asserted too: a token minted BEFORE this feature carries no
`gen` claim and must read as generation 0, so a deploy doesn't mass-logout every
live session (the default column value is also 0).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import update
from starlette.websockets import WebSocketDisconnect

from aiko_gateway.config import settings
from aiko_gateway.domain import auth_session, security, users_service
from aiko_gateway.domain.models import RefreshSession
from aiko_gateway.realtime import ws as ws_module
from aiko_gateway.realtime.hub import Hub
from aiko_gateway.rest import auth as auth_routes
from aiko_gateway.rest.deps import get_current_user

pytestmark = pytest.mark.asyncio


async def _user(session, name="alice"):
    return await users_service.create_user(
        session, username=name, display_name=name.title(), password="pw")


async def _bump_generation(session, user, to: int) -> None:
    """Simulate a revocation: advance the user's token_generation and commit so a
    fresh SessionLocal (the WS ingress opens its own) sees the new value."""
    user.token_generation = to
    await session.commit()


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# -- 1. the claim round-trips + migration safety ----------------------------- #

async def test_issue_embeds_generation_and_decode_returns_it():
    token = security.issue_access("u1", gen=7)
    sub, gen = security.decode_token(token, expected_type="access")
    assert sub == "u1"
    assert gen == 7


async def test_absent_gen_claim_reads_as_zero():
    """A token minted before this feature has no `gen` claim. It must decode to
    generation 0 so it still validates against the default token_generation=0 —
    no mass-logout on deploy. (An attacker can't strip the claim: HS256 signs it.)"""
    now = dt.datetime.now(dt.timezone.utc)
    legacy = jwt.encode(
        {"sub": "u1", "type": "access", "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(hours=1)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    sub, gen = security.decode_token(legacy, expected_type="access")
    assert sub == "u1"
    assert gen == 0


@pytest.mark.parametrize("bad_gen", ["not-int", None, -1, True, 1.5])
async def test_malformed_gen_claim_fails_closed(bad_gen):
    """decode_token is the trust-boundary parser: a signed-but-malformed `gen`
    (string / JSON-null / negative / bool / float) must raise InvalidTokenError, not
    a raw ValueError/TypeError that escapes the ingress try/except as a 500. Only we
    mint these (HS256), so this is defense-in-depth — same fail-closed posture as
    validate_origin. Cage-match Carnot+Tesla, PR#94."""
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode(
        {"sub": "u1", "type": "access", "gen": bad_gen, "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(hours=1)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_token(tok, expected_type="access")


# -- 2. REST ingress: get_current_user --------------------------------------- #

async def test_get_current_user_rejects_stale_generation(session):
    user = await _user(session)
    stale = security.issue_access(user.id, gen=0)   # gen at issue time
    await _bump_generation(session, user, to=1)     # revoke everything at gen 0

    with pytest.raises(HTTPException) as ei:
        await get_current_user(_creds(stale), session)
    assert ei.value.status_code == status.HTTP_401_UNAUTHORIZED

    fresh = security.issue_access(user.id, gen=1)
    got = await get_current_user(_creds(fresh), session)
    assert got.id == user.id


# -- 3. Refresh ingress ------------------------------------------------------ #

async def test_refresh_rejects_stale_generation_and_remints_current(session):
    user = await _user(session)
    stale_refresh = security.issue_refresh(user.id, gen=0)
    await _bump_generation(session, user, to=1)

    with pytest.raises(HTTPException) as ei:
        await auth_routes.refresh(
            auth_routes.RefreshReq(refresh_token=stale_refresh), session)
    assert ei.value.status_code == status.HTTP_401_UNAUTHORIZED

    fresh_refresh = security.issue_refresh(user.id, gen=1)
    out = await auth_routes.refresh(
        auth_routes.RefreshReq(refresh_token=fresh_refresh), session)
    # The re-minted access token must carry the CURRENT generation, so it is
    # accepted downstream (not a token that will itself be rejected).
    _, gen = security.decode_token(out["access_token"], expected_type="access")
    assert gen == 1


# -- 4. WS handshake ingress (the one the prose fix-list omitted) ------------- #

async def _run_handshake(session, monkeypatch, token):
    """Drive ws_endpoint against a stub socket, returning (accepted, close_code).
    Mirrors test_moderation_actions._run_handshake; the WS opens its OWN
    SessionLocal, so we monkeypatch it to the test session."""
    class _CM:
        async def __aenter__(self): return session
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(ws_module, "SessionLocal", lambda: _CM())

    class _StubWS:
        def __init__(self, tok):
            self.query_params = {"token": tok}
            self.app = SimpleNamespace(
                state=SimpleNamespace(gw=SimpleNamespace(hub=Hub())))
            self.closed_code = None
            self.accepted = False
        async def close(self, code=1000): self.closed_code = code
        async def accept(self): self.accepted = True
        async def receive_json(self): raise WebSocketDisconnect()

    stub = _StubWS(token)
    await ws_module.ws_endpoint(stub)
    return stub.accepted, stub.closed_code


async def test_ws_handshake_rejects_stale_generation(session, monkeypatch):
    user = await _user(session)
    stale = security.issue_access(user.id, gen=0)
    await _bump_generation(session, user, to=1)

    accepted, code = await _run_handshake(session, monkeypatch, stale)
    assert accepted is False and code == status.WS_1008_POLICY_VIOLATION

    fresh = security.issue_access(user.id, gen=1)
    accepted, code = await _run_handshake(session, monkeypatch, fresh)
    assert accepted is True and code is None


# -- 5. refresh rotation (#420): a session slides; a copy ends it ------------- #
#
# The rule (auth_session.rotate_refresh): presenting the CURRENT token rotates;
# the PREVIOUS one is a retry (a lost response) and replaces the current one;
# anything older is a second holder and ends the session. No clock is involved.

async def _refresh(session, token: str) -> dict:
    return await auth_routes.refresh(
        auth_routes.RefreshReq(refresh_token=token), session)


async def _refused(session, token: str) -> None:
    with pytest.raises(HTTPException) as ei:
        await _refresh(session, token)
    assert ei.value.status_code == status.HTTP_401_UNAUTHORIZED


def _jti(token: str) -> str:
    return security.refresh_ids(token)[1]


async def test_refresh_returns_a_successor_with_a_fresh_expiry(session):
    """The bug: the refresh token's 30 days were fixed at sign-in. Now each refresh
    returns a successor whose expiry is counted from the refresh, not the sign-in."""
    user = await _user(session)
    signed_in = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    t0 = security.issue_refresh(user.id, gen=0, now=signed_in)
    out = await _refresh(session, t0)
    old_exp = jwt.decode(t0, options={"verify_signature": False})["exp"]
    new_exp = jwt.decode(out["refresh_token"], options={"verify_signature": False})["exp"]
    assert new_exp - old_exp >= 10 * 24 * 3600 - 5
    assert security.refresh_ids(out["refresh_token"])[0] == security.refresh_ids(t0)[0]
    assert (await _refresh(session, out["refresh_token"]))["refresh_token"]


async def test_a_chain_of_successors_each_refreshes(session):
    user = await _user(session)
    token = security.issue_refresh(user.id, gen=0)
    seen = {_jti(token)}
    for _ in range(4):
        token = (await _refresh(session, token))["refresh_token"]
        assert _jti(token) not in seen
        seen.add(_jti(token))


async def test_an_app_that_ignores_the_new_field_keeps_working(session):
    """Every installed app before aiko_chat_app#427 keeps presenting the token it
    signed in with. That token stays the session's PREVIOUS one, so each refresh is
    a retry, and the island can deploy first."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    for _ in range(3):
        assert (await _refresh(session, t0))["access_token"]


async def test_a_lost_response_is_a_retry_hours_later(session):
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    lost = (await _refresh(session, t0))["refresh_token"]
    kept = (await _refresh(session, t0))["refresh_token"]       # the retry
    assert _jti(kept) != _jti(lost)
    t2 = (await _refresh(session, kept))["refresh_token"]
    assert t2


async def test_two_holders_end_the_session(session, caplog):
    """A thief and the owner both hold T0. Whoever refreshes second replaces the
    first one's successor, so the first one's next refresh presents a token older
    than the previous one: the session ends for both, and the island says so
    without naming whose session it was."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    owner = (await _refresh(session, t0))["refresh_token"]
    thief = (await _refresh(session, t0))["refresh_token"]
    await _refused(session, owner)
    await _refused(session, thief)
    await _refused(session, t0)
    ended = [r for r in caplog.records if "session has ended" in r.getMessage()]
    assert len(ended) == 1
    assert user.id not in ended[0].getMessage()


async def test_a_copy_used_after_its_successor_ends_the_session(session):
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    t1 = (await _refresh(session, t0))["refresh_token"]
    t2 = (await _refresh(session, t1))["refresh_token"]
    await _refused(session, t0)          # older than the previous one (t1)
    await _refused(session, t2)          # and the session is over


async def test_holders_refreshing_in_step_are_still_told_apart(session):
    """Tesla's phase lock (cage-match round 1, PR #450): with a re-issue window, two
    holders refreshing seconds apart were handed the same successor every time, and
    a copy never showed. A retry now always replaces the current token, so the
    holders fork at once and the one left behind ends the session."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    owner = (await _refresh(session, t0))["refresh_token"]
    thief = (await _refresh(session, t0))["refresh_token"]   # seconds later
    assert _jti(owner) != _jti(thief)
    await _refused(session, owner)
    await _refused(session, thief)       # the session is over for both


async def test_overlap_keeping_the_other_response_also_works(session):
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    await _refresh(session, t0)
    second = (await _refresh(session, t0))["refresh_token"]
    assert (await _refresh(session, second))["refresh_token"]


async def test_a_pre_420_refresh_token_upgrades_and_cannot_be_reused(session):
    """A token minted before rotation has no sid/jti. It still works (no mass-logout
    on deploy), its first refresh starts a rotating session, and presenting it after
    its successor was used ends that session like any other copy."""
    user = await _user(session)
    now = dt.datetime.now(dt.timezone.utc)
    legacy = jwt.encode(
        {"sub": user.id, "type": "refresh", "gen": 0, "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(days=30)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    t1 = (await _refresh(session, legacy))["refresh_token"]
    assert security.refresh_ids(t1)[0] == security.refresh_ids(legacy)[0]
    await _refresh(session, t1)
    await _refused(session, legacy)


async def test_a_respelled_pre_420_token_is_the_same_session(session):
    """Carnot, cage-match round 2: PyJWT accepts a token with trailing base64
    padding, so one legacy token has several spellings. Its session id comes from
    the decoded signature, which every spelling shares, so a respelled copy is
    judged in the owner's chain instead of starting one of its own."""
    user = await _user(session)
    now = dt.datetime.now(dt.timezone.utc)
    legacy = jwt.encode(
        {"sub": user.id, "type": "refresh", "gen": 0, "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(days=30)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    respelled = legacy + "="
    assert security.refresh_ids(respelled)[0] == security.refresh_ids(legacy)[0]
    t1 = (await _refresh(session, legacy))["refresh_token"]
    await _refresh(session, t1)                      # legacy is now older than previous
    await _refused(session, respelled)


async def test_a_lowered_ttl_never_shortens_a_sessions_horizon(session, monkeypatch):
    """Noether + Carnot, cage-match round 2: the row's `expires_at` must outlive
    every token it names. A token minted under a 30-day TTL still verifies for 30
    days after the TTL is lowered, so a row recomputed from the new setting would be
    pruned early and that token's next refresh would start the session again."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)       # 30-day exp
    t0_exp = security.refresh_ids(t0)[2]
    sid = security.refresh_ids(t0)[0]
    monkeypatch.setattr(settings, "jwt_refresh_ttl_seconds", 24 * 3600)
    t1 = (await _refresh(session, t0))["refresh_token"]
    row = await session.get(RefreshSession, sid, populate_existing=True)
    assert row.expires_at >= t0_exp
    await _refresh(session, t1)                       # a rotation under the short TTL
    row = await session.get(RefreshSession, sid, populate_existing=True)
    assert row.expires_at >= t0_exp


async def test_a_concurrent_first_refresh_is_judged_against_the_rivals_row(session):
    """Noether, cage-match round 2: two first refreshes of one session race to
    create its row. The loser's insert does nothing and it must judge its token
    against the winner's row. If the skipped insert were reported as a success, it
    would return a token the row never names, and that token's next refresh would
    end the session: the final assertion is what would catch it."""
    from sqlalchemy.sql.dml import Insert

    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    sid, t0_jti, t0_exp = security.refresh_ids(t0)
    real_execute = session.execute
    fired = []

    async def rival_first(stmt, *args, **kwargs):
        if (not fired and isinstance(stmt, Insert)
                and stmt.table.name == RefreshSession.__tablename__):
            fired.append(True)
            await real_execute(Insert(RefreshSession).values(
                id=sid, user_id=user.id, current_jti="rival",
                previous_jti=t0_jti, expires_at=t0_exp))
        return await real_execute(stmt, *args, **kwargs)

    session.execute = rival_first
    try:
        out = await _refresh(session, t0)
    finally:
        del session.execute
    assert fired, "the rival insert never ran"
    row = await session.get(RefreshSession, sid, populate_existing=True)
    assert row.previous_jti == t0_jti
    assert row.current_jti == _jti(out["refresh_token"]) != "rival"


async def test_ending_one_session_leaves_the_others(session):
    user = await _user(session)
    phone = security.issue_refresh(user.id, gen=0)
    desktop = security.issue_refresh(user.id, gen=0)
    a = (await _refresh(session, phone))["refresh_token"]
    await _refresh(session, phone)                   # replaces `a`
    await _refused(session, a)                       # phone's session ends
    assert (await _refresh(session, desktop))["refresh_token"]


async def test_a_rotated_token_still_dies_with_its_generation(session):
    user = await _user(session)
    t1 = (await _refresh(session, security.issue_refresh(user.id, gen=0)))["refresh_token"]
    await _bump_generation(session, user, to=1)
    await _refused(session, t1)


async def test_a_malformed_session_claim_is_a_401_not_a_500(session):
    user = await _user(session)
    now = dt.datetime.now(dt.timezone.utc)
    bad = jwt.encode(
        {"sub": user.id, "type": "refresh", "gen": 0, "sid": 7, "jti": "",
         "iat": int(now.timestamp()),
         "exp": int((now + dt.timedelta(days=30)).timestamp())},
        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    await _refused(session, bad)


async def test_an_ended_session_stays_ended_until_its_tokens_expire(session):
    """The row of an ended session is a tombstone. Pruned while any of its tokens
    could still verify, a copy's next refresh would look like a FIRST refresh and
    bring the session back. Pruning waits for the newest token's expiry plus slack,
    and only then removes it."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    t1 = (await _refresh(session, t0))["refresh_token"]
    await _refresh(session, t1)
    await _refused(session, t0)
    sid = security.refresh_ids(t0)[0]

    other = (await _refresh(session, security.issue_refresh(user.id, gen=0)))
    assert other["refresh_token"]                    # a refresh that prunes
    row = await session.get(RefreshSession, sid, populate_existing=True)
    assert row is not None and row.revoked_at is not None

    row.expires_at = (dt.datetime.now(dt.timezone.utc)
                      - auth_session._PRUNE_SLACK - dt.timedelta(minutes=1))
    await session.commit()
    await _refresh(session, other["refresh_token"])
    assert await session.get(RefreshSession, sid, populate_existing=True) is None


async def test_a_row_inside_the_slack_is_not_pruned(session):
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    await _refresh(session, t0)
    sid = security.refresh_ids(t0)[0]
    row = await session.get(RefreshSession, sid)
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    await session.commit()
    await _refresh(session, security.issue_refresh(user.id, gen=0))
    assert await session.get(RefreshSession, sid, populate_existing=True) is not None


async def test_a_lost_race_rereads_and_judges_the_new_state(session, monkeypatch):
    """Between this refresh's read and its write, another request rotated the
    session. The compare-and-swap misses, the row is read again, and the token is
    judged against the new state: it is now the PREVIOUS one, a retry, so it
    replaces the rival's successor rather than ending the session or overwriting
    the state it first read."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    t1 = (await _refresh(session, t0))["refresh_token"]   # the row exists; t1 current
    sid = security.refresh_ids(t0)[0]

    real_swap = auth_session._swap
    fired = []

    async def racing_swap(sess, row, **values):
        if not fired:
            fired.append(True)
            # synchronize_session=False: the rival is ANOTHER request, on another
            # connection, so it must not touch this request's in-memory row. With
            # the default, the ORM syncs `row` to the rival's state and the swap
            # compares against it and wins: a race test that cannot lose the race.
            await sess.execute(
                update(RefreshSession).where(RefreshSession.id == sid)
                .values(previous_jti=row.current_jti, current_jti="rival")
                .execution_options(synchronize_session=False))
        return await real_swap(sess, row, **values)

    monkeypatch.setattr(auth_session, "_swap", racing_swap)
    out = await _refresh(session, t1)
    assert fired, "the race injection never ran"
    row = await session.get(RefreshSession, sid, populate_existing=True)
    assert (row.previous_jti, row.current_jti) == (_jti(t1), _jti(out["refresh_token"]))
    assert _jti(out["refresh_token"]) != "rival"


async def test_losing_every_race_is_a_retry_not_a_sign_out(session, monkeypatch):
    """A refresh that loses every compare-and-swap never judged its token, so it
    must not answer with the 401 a client signs out on: it is a 503 to retry, and
    the token still works afterwards."""
    user = await _user(session)
    t0 = security.issue_refresh(user.id, gen=0)
    t1 = (await _refresh(session, t0))["refresh_token"]

    real_swap = auth_session._swap
    lost = []

    async def always_loses(sess, row, **values):
        lost.append(True)
        return False

    monkeypatch.setattr(auth_session, "_swap", always_loses)
    with pytest.raises(HTTPException) as ei:
        await _refresh(session, t1)
    assert len(lost) == auth_session._MAX_ATTEMPTS, "the lost-race injection never ran"
    assert ei.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert ei.value.headers == {"Retry-After": "1"}

    monkeypatch.setattr(auth_session, "_swap", real_swap)
    assert (await _refresh(session, t1))["refresh_token"]
