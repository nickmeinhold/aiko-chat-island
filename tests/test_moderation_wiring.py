"""A5 (crucible-09) machinery-present tripwire.

"Moderator = commitment" (config._harden_for_production) rests on the moderation
machinery actually BEING there — a report queue that accepts reports and act-on-report
routes (take-down → the #7 retraction path) that a configured moderator can reach. Today
that machinery is compile-time-present: the moderation router carries those routes and
main.py includes it UNCONDITIONALLY (no feature flag). This test promotes that prose
invariant to an introspection guard, so a future refactor that strips a route or makes the
include conditional fails CI here — instead of silently shipping a production island that
elected `moderator` mode but whose report queue nobody can reach.

This is the repo's cascade-completeness → runtime-guard pattern: a "verify the neighbour"
comment fails silently on the Nth change; an introspection tripwire does not.

Deliberately NOT built by importing the main `app`: importing aiko_gateway.main to inspect
its route table is the repo's documented test-isolation fragility (bus imports, the
lifespan, and settings-at-import make a bare route-table read unreliable — cf.
test_islands_directory.py, which builds a fresh FastAPI() rather than import main). Instead
we assert (a) the moderation ROUTER exposes the required routes, and (b) main.py includes
that router at module level (unconditionally) via a source check — together they pin
"the machinery exists AND is wired in" without the fragile import.

CEILING (PR#113 cage-match, Tesla): this is a "don't strip the wire" guard, NOT a
"commitment still has teeth" proof. It asserts route PATHS + the module-level include; it
does NOT assert HTTP methods, that `resolve` still mints the #7 retraction, that
`require_moderator` still guards the act-on-report routes, or that a rename/re-export
keeps the include semantically live. Those are behavioural guarantees pinned by the
moderation SERVICE/route tests, not this structural tripwire.
"""
from __future__ import annotations

import re
from pathlib import Path

from aiko_gateway.rest import moderation as moderation_routes

# The three capabilities A5's commitment depends on (router-relative paths; the router
# carries prefix="/v1"). Report CREATE (anyone can flag), the report QUEUE (list pending),
# and ACT-on-report (resolve = take-down, which mints the #7 retraction).
_REQUIRED_ROUTES = {
    "/v1/messages/{message_id}/report",
    "/v1/reports",
    "/v1/reports/{report_id}/resolve",
}


def test_moderation_router_exposes_the_commitment_routes():
    paths = {getattr(r, "path", "") for r in moderation_routes.router.routes}
    missing = _REQUIRED_ROUTES - paths
    assert not missing, (
        f"moderation router is missing route(s) A5 commits the operator to: "
        f"{sorted(missing)}. 'moderator = commitment' requires the report queue + "
        f"act-on-report machinery to exist; restore them in rest/moderation.py."
    )


def test_main_includes_the_moderation_router_unconditionally():
    # A module-level (unindented) include — not inside an `if`/feature flag. If a refactor
    # conditionalises it, this anchored match fails and the reviewer must re-affirm that
    # moderation is unconditionally wired (A5 / config._harden_for_production).
    main_src = (Path(__file__).resolve().parent.parent
                / "src" / "aiko_gateway" / "main.py").read_text()
    assert re.search(r"^app\.include_router\(moderation_routes\.router\)", main_src, re.M), (
        "main.py no longer includes the moderation router at module level "
        "(unconditionally). 'moderator = commitment' requires the moderation machinery to "
        "be wired for every boot — if this became conditional, A5's guarantee is hollow."
    )
