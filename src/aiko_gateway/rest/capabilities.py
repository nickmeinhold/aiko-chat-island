"""Token-less capability discovery — ``GET /capabilities`` (#4484).

What a client may learn about this island's WIRE BEHAVIOUR before it authenticates,
so no client ever has to carry a hand-maintained list of which hosts do what.

## Why this endpoint exists, in one measured fact

The app gates sovereign `origin` emit on "does this gateway carry the envelope?".
While this endpoint 404'd, that question was answered by a hardcoded Dart constant
(`kKnownCarriageHosts`). `chat.enspyr.co` was never in it, so from 2026-08-10 the
client STRIPPED the signature from every message sent there; an unsigned invite
fails the app's `admitRing` as `unverifiedOrigin`, and **no call to that island
could be answered by anyone, on any platform, for seven weeks.** Neither side could
complain: absent `origin` is legal on the wire (only a *malformed* envelope is
refused), and withholding is what the client gate is for. The evidence was a single
WARNING in the recipient's ring buffer.

The bug was not the missing entry. It was that a client had to hold a list of
production hosts it does not own — so a newly stood-up island is silently
uncallable until a human edits a constant in another repo.

## The contract, which the CLIENT already shipped and this must match exactly

```json
{"carriage": {"origin": true}}
```

The client's parse is deliberately THREE-state (`carriage_capability.dart`):
an explicit JSON boolean at `carriage.origin` is authoritative; **anything else —
404, a network error, `{}`, `{"carriage": {}}`, a non-bool — is "unknown" and falls
back to the allowlist seed.** A stub 200 is therefore indistinguishable from this
endpoint not existing, and just as silent. `test_capabilities.py` pins the emitted
document against a re-implementation of that parse, with stub documents as the
must-fail arm, because "the endpoint answers 200" is not the property that matters.

## What it deliberately does NOT disclose

One boolean about carriage. No identity, no operator, no counts, no posture — the
signed self-manifest (`GET /v1/island`) is where anything provenance-bearing
belongs, and per the app tab's ADR-0008 a field that names a PERSON or asserts
accountability may only be rendered from a signature the device verifies itself.
An unauthenticated endpoint that grew a population number would be a disclosure
decision made by accident; the key-set test exists to make growth deliberate.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

router = APIRouter(tags=["capabilities"])

#: This island carries the sovereign `origin` envelope: `realtime/ws.py` shape-
#: validates it at the trust boundary and passes it VERBATIM to
#: `messages_service.create_outbound`, which persists it, and `message_view` echoes
#: it to every reader — verifier-sufficient end to end. There is no configuration
#: that turns carriage off, which is exactly why this is a constant and not a
#: settings read: a dial would be a claim about a lever that does not exist.
#: A constant, though, is a fact about the code that can outlive the code, so it is
#: bound to the observed round-trip in `test_capabilities.py` rather than asserted
#: on its own authority.
CARRIES_ORIGIN = True


@router.get("/capabilities")
async def get_capabilities(response: Response) -> dict:
    """This island's wire capabilities. Public, token-less, no side effects —
    a sibling of `/health`, fetched by the app on every (re)connect."""
    # The answer changes only on DEPLOY, and a proxy holding the previous build's
    # copy across one is precisely the stale window this endpoint exists to close
    # (a cached `false`, or a cached 404 replayed as unknown, re-arms the client's
    # allowlist fallback on an island that has started carrying).
    response.headers["Cache-Control"] = "no-store"
    return {"carriage": {"origin": CARRIES_ORIGIN}}
