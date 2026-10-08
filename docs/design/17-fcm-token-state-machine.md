# Design 17: FCM's OAuth token is a state machine, so make it one

**Status:** RECAST round 1 (2026-10-08), awaiting a ≥2-family re-strike. Written 2026-10-06 because
PR#192's cage-match reached its three-round cap, and one finding class surfaced in every round. The first
temper ([`17-TEMPER.md`](17-TEMPER.md)) seated only Claude and Grok, so it is **UN-TEMPERED
(provisional)**. Both said RECAST, and v1's proposal is kept below, struck, because what it lacked is
the content.

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

## Proposal v2 (recast after temper round 1)

**The name `_TokenCell` is retired.** It named the bearer cache, which left send-side authorization
with no home, and that is exactly how the biggest gap below hid. The unit is a **pure function**,
`transition(state, event) -> state`, in the `plan_deliveries` shape. A thin holder around one state
value is optional sugar.

### What the temper measured that v1 did not know

- **IAM is checked at SEND, never at mint.** Measured 2026-10-08: a service account with no role
  minted a token fine, then got 403 on send. Revocation is now an intended operator move (one ring
  account per island, `ring-<island>`, role `islandRinger` = `cloudmessaging.messages.create`),
  and it shows up ONLY on the send path.
- **The island-wide 403 is bare:** gRPC status `PERMISSION_DENIED` with no FcmError `errorCode`.
  `SENDER_ID_MISMATCH` is the 403 that carries an FcmError code, and it is per-device.
- **The fanout is concurrent** (`asyncio.gather` in `push_service`), so a cold cache under a ring to
  N Android devices is N concurrent mints, not "one wasted exchange".
- **A seconds-old key 400s at mint** (propagation lag), and so do a deleted key and a skewed clock.
  All three are OAuth 400 `invalid_grant`, with three different remedies.

### States, carrying their clocks

`empty` · `cached(token, expires_at)` · `mint_backoff(kind, until)` · `send_denied(until)`

### Events: the codomain of ONE total classifier

The events are not a list someone writes down. They are the outputs of a total classifier over
`(endpoint ∈ {mint, send}) × status × error code`, and the sweep runs over that codomain. A
response nobody anticipated lands in **`unclassified`**, which logs loudly and is tested to be
non-silent, never silently in REJECTED.

| event | from |
|---|---|
| `minted(token, expires_in)` | mint 200 with a non-empty `str` token and a non-negative lifetime. Any other 200 is `mint_failed(unreadable)` **inside the classifier**, not after it |
| `mint_failed(credential)` | parse/sign failure, before any network |
| `mint_failed(grant)` | OAuth 400 `invalid_grant` (propagation, deleted key, skew), read from the fixed `error` field. The body never reaches the log |
| `mint_failed(unreadable)` | 200 with a bad body, or a non-200 that is not `grant` |
| `blinked` | mint or send transport error, 429, 5xx |
| `refused(bearer)` | send 401 |
| `denied` | send 403 + `PERMISSION_DENIED` + no FcmError code |
| `tick(now)` | time passing: cache expiry, backoff lapse |
| `unclassified` | anything else |

### `get` is tri-state, and the mint is single-flight

`get(state, now) -> Have(token) | Mint | Silent`. Only `Mint` may POST the token endpoint, and the
POST is **single-flight**: one in-flight future that every concurrent caller awaits. That removes the
self-inflicted 429 burst (old question 2), and with it most of the reason a blink would need a
backoff.

### The table

| state × event | `minted` | `mint_failed(credential\|unreadable)` | `mint_failed(grant)` | `blinked` | `refused(b)` | `denied` | `tick` past deadline |
|---|---|---|---|---|---|---|---|
| empty | cached | mint_backoff(long) | mint_backoff(short) | — | — | send_denied | — |
| cached(t, e) | replace | mint_backoff(long) | mint_backoff(short) | — | empty iff b == t | send_denied | empty |
| mint_backoff | cached | extend | extend(short) | — | — | send_denied | empty |
| send_denied | **stays send_denied** | stays | stays | — | — | extend | empty |

The bold cell is the one v1 could not express: a fresh token from an account that lost its role
403s again, so minting must not end the denial. Only time (or a config reload) does.

`get` from `send_denied` or `mint_backoff` returns `Silent`: no per-device POST and no re-mint. From
`empty`, or from a `cached` past expiry, it returns `Mint`. A device under any island-wide state gets
TRANSIENT, never REJECTED: the device is not implicated.

### Operator messages, one per layer

- `mint_failed(grant)`: "the key was refused: it may be minutes old (propagation), deleted or
  disabled, or this box's clock may be off". Not the role.
- `denied`: "this island's ring account may not send: check its role (`islandRinger`) and that the
  Cloud Messaging API is enabled". This is the one place the role belongs.

### Owed before build (measurements, not claims)

1. **Revocation latency:** time from disabling `ring-<island>` (and separately, deleting its key) to
   the first refused send, and which event it arrives as (`refused` or `denied`). This tells us
   whether the ~55 min cache outlives a revocation, and it doubles as the runbook for ejecting an
   island's ringer.
2. **Short vs long backoff values**, justified against the measured propagation time (≤30s on
   2026-10-08).

### v1 (struck, kept for the record)

v1 proposed a mutable `_TokenCell` with `get / minted / mint_failed(credential|refused|blinked|
unreadable) / refused(bearer)` over `{empty, cached, backoff}`, swept with `product(states, events)`.
It had no send-side authorization state, no time, a `None` that meant three things, and an enum that
the sweep could only certify, not complete. See `17-TEMPER.md`.

## Questions for the re-strike

1. Is `send_denied` exited by time alone correct, or should a config reload (new credential) be an
   explicit event?
2. Does single-flight need a timeout of its own, so that one hung mint cannot silence every waiter
   past the client's 10s?
3. Old Q3, answered: APNs is NOT this table. It shares the event algebra (Apple's
   `ExpiredProviderToken` is the `refused(bearer)` analogue), and gets its own one-writer grid only if
   a finding asks for one.

## Not in scope

The wire contract (`{c, k, m}`), routing, and the call/2 parser. Those closed cleanly across three
rounds.
