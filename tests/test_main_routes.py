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

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

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


def test_main_mounts_auth_only_through_include_auth_routers():
    """A STRUCTURAL TRIPWIRE, because the behaviour cannot be observed here. This
    suite imports `aiko_gateway.main` once, under ENVIRONMENT=test, so the real app
    always mounts the dev-only password door and no behavioural test of `main` can
    see what PRODUCTION mounts. `include_auth_routers` is tested under both
    environments (test_register_gate.py); this pins that main.py reaches the auth
    routers only through it. A direct `app.include_router(auth_routes.dev_router)`
    in main would serve /register in production while every other test stayed
    green (cage-match PR#433)."""
    import inspect

    import aiko_gateway.main as main_module

    source = inspect.getsource(main_module)
    assert source.count("include_auth_routers(app)") == 1
    # The bare identifier, not one spelling of it: `from .rest.auth import
    # dev_router` (or any alias of the module) still has to name it.
    assert "dev_router" not in source, "main.py names dev_router; mount it only via include_auth_routers"
    for line in source.splitlines():
        if "include_router(" in line:
            assert "auth" not in line, f"main.py mounts an auth router directly: {line.strip()}"


# --- the frozen wire contract (Dart port step 1, docs/crucible/dart-port §2.6) -- #

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contract"

# What PRODUCTION serves, asked of production itself. Every value is a throwaway that
# satisfies one of the production boot guards, not a setting under test: this suite
# imports `main` once under ENVIRONMENT=test, which mounts the dev-only password door,
# so the only honest way to see production's route table is a process that IS
# production. The bus points at a dead port and the update nudge is off, so the child
# never reaches the network. A new guard fails this test loudly, with its own refusal.
_PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    "PASSKEY_ENABLED": "true",
    "MODERATOR_USER_IDS": '["contract-placeholder"]',
    "CSAM_RUNBOOK_ACKNOWLEDGED": "true",
    "ISLAND_UPDATE_NUDGE": "off",
    "AIKO_MQTT_HOST": "127.0.0.1",
    "AIKO_MQTT_PORT": "9",
}

_DUMP_CONTRACT = """
import json, sys
from aiko_gateway.main import app

def flat(routes):
    # FastAPI >= 0.116 mounts an included router lazily (`_IncludedRouter`, real
    # routes under `.original_router`), so the parent's list is not flat.
    for r in routes:
        inner = getattr(getattr(r, "original_router", None), "routes", None)
        if inner is not None:
            yield from flat(inner)
        else:
            yield r

json.dump({
    "openapi": app.openapi(),
    "websockets": sorted({r.path for r in flat(app.routes)
                          if type(r).__name__ in ("WebSocketRoute", "APIWebSocketRoute")}),
}, sys.stdout)
"""


def _serialise(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_frozen_contract_is_what_production_serves(tmp_path):
    """`contract/openapi.json` is the cross-tab wire contract and the Dart gateway's
    target (§2.6), so it must be exactly what production serves today. FastAPI does
    not document WebSocket routes, so they are frozen beside it in
    `contract/websockets.json`: without that file a new socket route would be
    invisible to the journeys' orphan check.

    Regenerate after a deliberate wire change with
    `UPDATE_CONTRACT=1 pytest tests/test_main_routes.py -k frozen_contract`, and tell
    the app tab: the diff IS the wire change. Checked against both live islands on
    2026-11-10 (59 paths, byte-equal as JSON)."""
    env = {"PATH": os.environ.get("PATH", ""), **_PRODUCTION_ENV,
           "JWT_SECRET": secrets.token_hex(32),
           "ISLAND_SIGNING_SEED": base64.urlsafe_b64encode(
               secrets.token_bytes(32)).rstrip(b"=").decode()}
    # cwd=tmp_path: Settings reads `.env` from the working directory, and a
    # developer's dev `.env` must not leak into what "production" means.
    proc = subprocess.run([sys.executable, "-c", _DUMP_CONTRACT], env=env,
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"production refused to build its app:\n{proc.stderr[-3000:]}"
    served = json.loads(proc.stdout)

    files = {"openapi.json": served["openapi"], "websockets.json": served["websockets"]}
    if os.environ.get("UPDATE_CONTRACT") == "1":
        CONTRACT_DIR.mkdir(exist_ok=True)
        for name, obj in files.items():
            (CONTRACT_DIR / name).write_text(_serialise(obj), encoding="utf-8")
        return

    for name, obj in files.items():
        frozen = (CONTRACT_DIR / name).read_text(encoding="utf-8")
        if frozen == _serialise(obj):
            continue
        detail = ""
        if name == "openapi.json":
            old, new = json.loads(frozen)["paths"], obj["paths"]
            detail = (f"\n  added:   {sorted(set(new) - set(old))}"
                      f"\n  removed: {sorted(set(old) - set(new))}"
                      f"\n  changed: {sorted(p for p in set(old) & set(new) if old[p] != new[p])}")
        pytest.fail(f"contract/{name} is not what production serves.{detail}\n"
                    "If the change is deliberate, regenerate with UPDATE_CONTRACT=1 "
                    "(see this test's docstring) and tell the app tab.")
