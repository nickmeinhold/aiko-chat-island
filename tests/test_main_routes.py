"""Production-app wiring guard (task #37).

The HTTP auth tests in `test_rest_auth.py` build a minimal FastAPI app from JUST
the read routers — they deliberately do NOT import `aiko_gateway.main`, to keep
the suite's "never import aiko_services" isolation invariant. The cost of that
isolation: those tests can't catch a regression in the PRODUCTION wiring — if
`main.py` stopped mounting a router, or re-added an inline unauthenticated
`@app.get` for one of these paths, the router-only tests would stay green while
production silently regressed (no auth on a read endpoint = the exact I1
violation §A3 closed).

This module closes that gap by introspecting the REAL app object
(`aiko_gateway.main.app`). It is only importable because `main.py` imports
`AikoBusClient` lazily (inside `lifespan`), so `import aiko_gateway.main` no
longer pulls aiko_services at module scope. We assert here that this isolation
holds (the import itself would fail on clean CI otherwise) AND that both read
routes are mounted with `get_current_user` in their dependency tree.
"""
from __future__ import annotations

from aiko_gateway.main import app
from aiko_gateway.rest.deps import get_current_user

# The two read paths whose auth wiring this test guards (I1, plan §A3).
GUARDED_PATHS = {
    "/v1/channels",
    "/v1/channels/{channel_id}/messages",
}


def _iter_api_routes(routes):
    """Yield every APIRoute reachable from `routes`, flattening nested routers.

    FastAPI >= 0.116 mounts `include_router`ed routers lazily as `_IncludedRouter`
    wrappers (path=None) rather than copying their routes into `app.routes`; the
    real APIRoutes live under `wrapper.original_router.routes`. Older versions
    flatten directly. We recurse through any wrapper exposing `original_router`
    (or a nested `routes` list) so this guard is robust across both layouts.
    """
    for route in routes:
        if hasattr(route, "dependant") and getattr(route, "path", None) is not None:
            yield route
        nested = getattr(route, "original_router", None)
        nested_routes = getattr(nested, "routes", None)
        if nested_routes is None:
            nested_routes = getattr(route, "routes", None)
        # Guard against self-recursion: only recurse into a *different* route list.
        if nested_routes is not None and nested_routes is not routes:
            yield from _iter_api_routes(nested_routes)


def _route_for(path: str):
    """Return the mounted APIRoute for `path`, or None if it isn't mounted."""
    for route in _iter_api_routes(app.routes):
        if getattr(route, "path", None) == path:
            return route
    return None


def _dependency_calls(dependant) -> set:
    """Every callable in a route's full (recursive) dependency tree."""
    calls = set()
    stack = [dependant]
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


def test_production_app_mounts_guarded_paths():
    """main.py must mount both read endpoints — not drop the include_router."""
    mounted = {r.path for r in _iter_api_routes(app.routes)}
    missing = GUARDED_PATHS - mounted
    assert not missing, f"production app is missing routes: {missing}"


def test_guarded_paths_require_auth_dependency():
    """Each read route's dependency tree must include `get_current_user`.

    Catches a regression where a path is served by an inline, unauthenticated
    handler (the pre-§A3 I1 violation) — the route would be mounted but its
    dependant tree would NOT contain the auth dependency.
    """
    for path in GUARDED_PATHS:
        route = _route_for(path)
        assert route is not None, f"{path} is not mounted as an APIRoute"
        calls = _dependency_calls(route.dependant)
        assert get_current_user in calls, (
            f"{path} does not require auth: get_current_user is not in its "
            f"dependency tree (inline/unauthenticated handler regression?)"
        )
