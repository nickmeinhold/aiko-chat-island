# Design 17: FCM's OAuth token is a state machine, so make it one

**Status:** DRAFT, for `/design-temper`. Written 2026-10-06 because PR#192's cage-match reached its
three-round cap, and one finding class surfaced in every round.

## Why this exists

Nick's round-cap rule (cage-match Round 9.8) says: if a review is still finding real defects at round
3, escalate to a design pass, not to a round 4. The tell is a **repeated finding class**. PR#192 has one:

| Round | Seat | Finding (all fixed) | Transition it broke |
|---|---|---|---|
| 1 | Carnot | a send-side 401 never cleared the cached token | send → refused |
| 2 | Tesla | the mint negative-cached 429/5xx along with real refusals | mint → blinked |
| 2 | Tesla | a 200 with `access_token: null` was cached for ~55 min | mint → succeeded (bad shape) |
| 2 | Tesla | `error` read as a dict unchecked, so a string raised out of `send` | send → refused (bad shape) |
| 3 | Tesla | the 401 clear was unconditional, so a late 401 wiped a newer token | send → refused, concurrently |

The round-2 author did run a class sweep, and it still missed the round-3 instance. The sweep was a
list written down from memory, and the clearer's concurrency wasn't on it. That's the signal: the
defects are not in any one line, they come from the shape.

## The shape that produces them

The token is three pieces of **module-global mutable state** (`_cached_access_token`,
`_oauth_backoff_until`, and implicitly "the bearer this send used"). They have **three writers**:
- a successful mint;
- a failed mint, which sets the backoff;
- a send-side 401, which clears the cache.

Those writers are spread over two functions and coordinated by comments. Every fix so far has added a
guard at one writer: a status check, a type check, a compare. Each guard was correct, and the set of
transitions is still only *implied*. No table says what every (state, event) pair should do, so each
review round finds the next pair nobody wrote down.

This is the same shape `plan_deliveries` had before it became a pure function swept against
`itertools.product(...)`. Its fix was to turn the routing table into data and test the whole table.

## Proposal (to be tempered, not built)

A small `_TokenCell` that owns all three fields and exposes **events**, not field writes:

- `get(now) -> str | None`
- `minted(token, expires_in, now)`
- `mint_failed(kind, now)`, where `kind ∈ {credential, refused, blinked, unreadable}`
- `refused(bearer)`, which is compare-and-clear by construction

Its transitions are a literal table:

| state × event | `minted` | `mint_failed: credential/refused/unreadable` | `mint_failed: blinked` | `refused(b)` |
|---|---|---|---|---|
| empty | cache | backoff | — | — |
| cached(t) | replace | backoff | — | clear iff b == t |
| backoff | cache, end backoff | extend | — | — |

The suite then sweeps `product(states, events)` against the table, exactly as `test_push_routing.py`
sweeps the router. A new event or state then fails the sweep instead of waiting for a reviewer.

## Questions for the temper

1. Is the cell worth it for ~40 lines of logic, or is the table alone (as a test, over the current
   functions) enough to close the class?
2. Should `blinked` back off at all? It currently doesn't, and a concurrent-mint burst can hit 429
   repeatedly. A short jittered backoff might be right where a 60s one was wrong.
3. Does APNs' provider-token cache have the same shape? It signs locally, so it has fewer writers, but
   it is checked against the same table.

## Not in scope

The wire contract (`{c, k, m}`), routing, and the call/2 parser. Those closed cleanly across three
rounds.
