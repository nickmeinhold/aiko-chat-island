# Design 17: FCM's OAuth token is a state machine, so make it one

**Status:** v4 (2026-10-08), after three temper rounds; the round cap is reached. Round 3 was the first with two outside families on one version (Gemini + Grok, both RECAST). v4 is SMALLER than v3. See `17-TEMPER.md`. Written 2026-10-06 because
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

## Proposal v4 (recast after temper rounds 1-3)

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

### Two things v3 added that v4 deletes, and why

- **`half_open` (a one-device probe) is gone.** It was my round-2 finding, and round 3 (Tesla)
  overturned it. Letting a whole ring through after the denial window costs one fanout of refused
  POSTs per window. That is the CHEAP side of the asymmetry `_verdict` already states: a wasted
  request is cheap, a destroyed wake is not. A one-device probe spends the expensive side instead,
  because the device list contains dead tokens. A probe that lands on one keeps the island dark
  through the first real call after the operator's fix.
- **`config_reloaded` is gone, because nothing could fire it** (Kelvin asked what does). Settings
  are read from the environment at boot. A new credential means a container restart, and a
  restart wakes this state as `empty`. **A restart IS the reload.** The same fact dissolves
  Tesla's generation counter: within one process the account cannot change, so a `denied` is
  always true about THIS account. What remains is staleness in TIME, handled by `sent_at` below.

### State: one value, with clocks and a strike count

`(phase, strikes)`, where `phase` is one of `empty` · `cached(token, expires_at)` ·
`mint_backoff(until)` · `send_denied(until, opened_at)`.

`strikes` survives phase changes and resets only on `delivered`. It is what makes repeated failures
back off further. All clocks are `time.monotonic()`. Wall-clock skew only ever shows up inside
`mint_failed(grant)`. **A restart wakes `(empty, 0)`**, so the first ring after a boot during an IAM
outage pays one fanout. That is stated, and accepted.

### Events: the codomain of ONE total classifier

| event | from |
|---|---|
| `minted(token, expires_in)` | mint 200 with a non-empty `str` token and a non-negative lifetime. Any other 200 is `mint_failed(unreadable)` **inside the classifier** |
| `mint_failed(credential)` | parse/sign failure, before any network |
| `mint_failed(unreadable)` | 200 with a bad body, or a non-200 that is none of the below |
| `mint_failed(grant)` | OAuth 400 `invalid_grant` (propagation, deleted key, skew), read from the fixed `error` field. The body is never logged |
| `blinked` | transport error, 429 or 5xx, on either endpoint |
| `refused(bearer)` | send 401 |
| `denied(sent_at)` | send 403 + `PERMISSION_DENIED` + no FcmError code |
| `delivered` | send 200 |
| `device_local` | any per-device verdict (`UNREGISTERED`, `SENDER_ID_MISMATCH`, `INVALID_ARGUMENT`) |
| `tick(now)` | time passing |
| `unclassified` | anything else: logs loudly, tested non-silent, **never** routed to `denied` |

### The table: total, with no dashes

"keep" is an explicit no-op, and it is tested as one.

| phase × event | `minted` | `mint_failed(credential\|unreadable)` | `mint_failed(grant)` | `blinked` | `refused(b)` | `denied(s)` | `delivered` | `device_local` | `tick` past deadline | `unclassified` |
|---|---|---|---|---|---|---|---|---|---|---|
| empty | cached | mint_backoff(long), strikes+1 | mint_backoff(grow), strikes+1 | keep | keep | send_denied(grow), strikes+1 | keep, strikes=0 | keep | keep (no deadline) | keep, loud |
| cached(t, e) | replace | mint_backoff(long), strikes+1 | mint_backoff(grow), strikes+1 | keep | empty iff b == t, else keep | send_denied(grow), strikes+1 | keep, strikes=0 | keep | empty | keep, loud |
| mint_backoff(u) | cached | extend(long) | extend(grow) | keep | keep | send_denied(grow), strikes+1 | keep | keep | empty | keep, loud |
| send_denied(u, o) | **keep** | keep | keep | keep | keep | **keep iff s < o + window (coalesce)** | keep (stale 200) | keep | empty | keep, loud |

The two bold cells carry the whole fix:
- **`minted` does not end a denial.** A fresh token from an account that lost its role 403s again.
- **Coalescing.** The first ring after a revocation has N sends in flight, and they return N
  `denied`. The first one opens `send_denied`, and the other N−1 land on `keep`. One fact, one
  strike. A `denied` whose `sent_at` predates the current phase's open time (a late 403 from a
  ring before the window lapsed) is stale, and it is a no-op in every phase. It must not re-close a
  window that just reopened.

`grow` = `min(base × 2^(strikes−1), cap)`. Proposed values: `base` 10s for `grant` (propagation was
measured at ≤30s) and 60s for `denied`; `cap` 15 min for both; `long` = 60s flat. A permanently
deleted key therefore decays to one token POST per 15 min, not one per 10s. That is the answer to
Kelvin's "permanent failure on a short backoff" without paying a long backoff on every fresh key.

### `get` is tri-state, and the mint is single-flight

`get(state, now) -> Have(token) | Mint | Silent`. `Silent` from `mint_backoff` and `send_denied`.
`Mint` from `empty`, or from a `cached` past expiry. Only `Mint` POSTs, and the POST is
**single-flight**: one in-flight future that every concurrent caller awaits, bounded by the
client's existing 10s timeout. When the timeout fires, every waiter gets `blinked`. A device under a
`Silent` phase gets TRANSIENT, never REJECTED. The fanout does not retry, so a ring during `Silent`
is dropped, not parked. That is the cost of the window, stated.

### Visibility, without a restart loop

Android's auth phase is exposed on the island's capabilities/health output as **Android
not-ready** (not a name next to a green check) while the phase is `mint_backoff` or `send_denied`.
**It must NOT fail the container healthcheck.** Under `restart: always`, an IAM outage would
become a restart loop that takes the working APNs transport down with it. Design 14's temper
recorded exactly that failure for the boot guard.

### Scope

One gateway process (module-global state). Several workers would each learn the phase separately,
which is safe but not shared.

### Owed before build (measurements, not claims)

1. **Revocation latency:** time from disabling `ring-<island>` (and separately, deleting its key) to
   the first refused send, and which event it arrives as (`refused` or `denied`). This tells us
   whether the ~55 min cache outlives a revocation, and it doubles as the runbook for ejecting an
   island's ringer.
2. **Backoff values** (`base`, `cap`, `long` above), justified against the measured propagation time
   (≤30s on 2026-10-08).
3. **Disable the Cloud Messaging API alone** (Tesla, round 3), and record whether that arrives as a
   mint non-200 or as a send `denied`. That decides which operator message it wears.

### v1 (struck, kept for the record)

v1 proposed a mutable `_TokenCell` with `get / minted / mint_failed(credential|refused|blinked|
unreadable) / refused(bearer)` over `{empty, cached, backoff}`, swept with `product(states, events)`.
It had no send-side authorization state, no time, a `None` that meant three things, and an enum that
the sweep could only certify, not complete. See `17-TEMPER.md`.

## Resolved by the temper

- v1 Q1 (cell or table?): a pure `transition` plus a total table, swept.
- v1 Q2 (should blinked back off?): no. Single-flight removes the self-inflicted burst.
- v1 Q3 (APNs?): not this table.
- v3 Q1/Q2 (`send_denied` exit, single-flight timeout): exit by time into a normal ring with
  coalescing. Single-flight is bounded by the 10s client timeout.

## Not in scope

The wire contract (`{c, k, m}`), routing, and the call/2 parser. Those closed cleanly across three
rounds.
