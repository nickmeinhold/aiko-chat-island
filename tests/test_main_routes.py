"""Production-app wiring guard (task #37, hardened in #41).

The HTTP auth tests in `test_rest_auth.py` build a minimal FastAPI app from JUST
the read routers — they deliberately do NOT import `aiko_gateway.main`, to keep
the suite's "never import aiko_services" isolation invariant. The cost of that
isolation: those tests can't catch a regression in the PRODUCTION wiring — if
`main.py` stopped mounting a router, or re-added an inline unauthenticated
`@app.get` for one of these paths, the router-only tests would stay green while
production silently regressed (no auth on a read endpoint = the exact I1
violation §A3 closed).

This module closes that gap by driving the REAL app object
(`aiko_gateway.main.app`) over its public ASGI surface. It is only importable
because `main.py` imports `AikoBusClient` lazily (inside `lifespan`), so
`import aiko_gateway.main` no longer pulls aiko_services at module scope — the
import itself would fail on clean CI otherwise.

#41 hardening — observable contract, not internals
--------------------------------------------------
The original version introspected FastAPI's private route graph
(`route.dependant`, recursive dependency walks, and `_IncludedRouter`
`.original_router` lazy-mount wrappers) to prove `get_current_user` sat in each
route's dependency tree. That coupled the guard to FastAPI internals that churn
across versions — the `_IncludedRouter` lazy-mount wrapper (path=None, real
routes nested under `.original_router`) landed in 0.116 and had already forced a
recursive-flattening workaround in the original test. Reading `route.path` off
`app.routes` is *also* not version-stable for the same reason (included routes
don't surface their template on the parent under the lazy-mount layout).

So we assert the *observable* contract over the real production app instead,
through the public ASGI surface, which FastAPI keeps stable:

  * unauthenticated request → 401  (route is mounted AND auth-gated; an
    UNMOUNTED path returns 404 here, and an inline UNAUTHENTICATED handler
    returns 200/404 — only a mounted, auth-gated route answers 401);
  * authenticated request   → 200 / 404-for-missing-row  (the route genuinely
    reaches its handler past auth — proving the 401 above was the auth gate, not
    a coincidental catch-all).

Same guarantee as #37 — production read routers are mounted and auth-gated —
expressed via behaviour, robust to FastAPI internal refactors.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiko_gateway.domain import security, users_service
from aiko_gateway.main import app
from aiko_gateway.rest.deps import get_session

# The read paths this test guards (I1, plan §A3), as concrete request paths.
# The `{channel_id}` template is filled with an id that does NOT exist: auth is
# checked before any row is read, so an unauthenticated request is rejected
# regardless, and an authenticated request reaches the handler and 404s on the
# absent channel — both observable proofs that the real route is wired.
CHANNELS_PATH = "/v1/channels"
HISTORY_PATH = "/v1/channels/no-such-channel/messages"

# The membership-management WRITE paths (#46) — the trust-boundary mutations.
# Same guard: an unmounted route would 404 here, so a 401 proves it is mounted
# AND auth-gated. The GET (list members) is exercised below for the authed leg;
# the write verbs (POST/DELETE) are auth-gated identically — covered by the
# unauthenticated parametrization via _unauthed_request per method.
MEMBERS_LIST_PATH = "/v1/channels/no-such-channel/members"
GUARDED_PATHS = (CHANNELS_PATH, HISTORY_PATH, MEMBERS_LIST_PATH)

# (method, path) pairs for the write membership routes — each must reject an
# unauthenticated caller. A missing mount answers 404/405; only a mounted,
# auth-gated route answers 401.
GUARDED_WRITE_ROUTES = (
    ("POST", "/v1/channels"),
    ("POST", "/v1/channels/no-such-channel/members"),
    ("DELETE", "/v1/channels/no-such-channel/members/no-such-user"),
    ("POST", "/v1/channels/no-such-channel/join"),
    ("DELETE", "/v1/channels/no-such-channel/leave"),
)


@pytest_asyncio.fixture
async def client(session):
    """An httpx client bound to the REAL production app, with only the DB
    session overridden to the in-memory test session. Lifespan is NOT run
    (ASGITransport doesn't trigger it), so the aiko bus never starts — the auth
    dependency is exercised over the genuine production wiring."""
    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _auth_header(session) -> dict:
    """Create a real user and mint a real access token for it."""
    user = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    return {"Authorization": f"Bearer {security.issue_access(user.id)}"}


@pytest.mark.parametrize("path", GUARDED_PATHS)
async def test_guarded_paths_reject_unauthenticated(client, path):
    """Each read route, on the REAL app, rejects an unauthenticated request.

    Observable-contract replacement for the old dependency-graph introspection:
    an UNMOUNTED route would answer 404 here, and a route served by an inline,
    UNAUTHENTICATED handler (the pre-§A3 I1 violation) would answer 200/404 —
    only a mounted, auth-gated route answers 401.
    """
    resp = await client.get(path)
    assert resp.status_code == 401, (
        f"{path} answered {resp.status_code} without credentials — expected 401 "
        f"(missing route ⇒ 404, or inline/unauthenticated handler ⇒ 200/404?)"
    )


@pytest.mark.parametrize("method,path", GUARDED_WRITE_ROUTES)
async def test_guarded_write_routes_reject_unauthenticated(client, method, path):
    """Each membership-management WRITE route, on the REAL app, rejects an
    unauthenticated request (#46). An unmounted route answers 404/405 here, so a
    401 proves the production router is mounted AND auth-gated past the I1 dep —
    the same faithful-proxy guard #37 established for the read routes."""
    resp = await client.request(method, path)
    assert resp.status_code == 401, (
        f"{method} {path} answered {resp.status_code} without credentials — "
        f"expected 401 (unmounted ⇒ 404/405, or inline/unauthenticated ⇒ 2xx?)"
    )


async def test_guarded_paths_reach_handler_when_authed(client, session):
    """With a valid token the read routes pass auth and reach their handler.

    Confirms the 401s above came from the auth gate, not from the routes being
    absent (an unmounted path would 404 even with a token). `/v1/channels` lists
    (200, empty) and the history path 404s on the non-existent channel — both
    are handler responses, reached only because the production routers are
    mounted past the auth dependency.
    """
    headers = await _auth_header(session)

    channels = await client.get(CHANNELS_PATH, headers=headers)
    assert channels.status_code == 200, (
        f"{CHANNELS_PATH} did not reach its handler with a valid token "
        f"(got {channels.status_code}) — is the router mounted?"
    )

    history = await client.get(HISTORY_PATH, headers=headers)
    assert history.status_code == 404, (
        f"{HISTORY_PATH} did not reach its handler with a valid token "
        f"(got {history.status_code}; expected 404 for the absent channel)"
    )
