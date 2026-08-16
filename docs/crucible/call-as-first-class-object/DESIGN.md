# Design — a call as a first-class object (island half)

*Movement 3 (Cast), folded once (movement 4) before Temper. Status: **UN-TEMPERED** —
no cross-family strike has run. Do not build on this yet.*

## Problem

The LiveKit room is the channel id, so a DM has one room forever and **a call is not an
object** — it is a transient occupancy state of an eternal room. Four consequences we are
each separately patching around: liveness is unanswerable in principle, call history is
inexpressible, concurrent calls collide, and consent has to be faked with a DM-only gate
on both sides of the wire.

## The shape

**A call is a row. The room is named after the row. The client never names a room, and
the invite body never changes.**

### The synthesis that dissolves #3170's stated cost

#3170 assumed a per-call room forces a **v2 invite wire format**, because the invite must
carry the call id. It does not — *if the callee asks the island which call is live rather
than reading it out of the body.*

The invite says **"a call started here."** The island says **"here is the call."** The
body keeps carrying zero parameters, and `kCallInviteBody` — a one-way door already in
signed history on enspyr — is never reopened.

This also means **#3159's endpoint shape survives almost unchanged**; it just returns a
call object instead of raw occupancy. The two proposals were never really in conflict:
#3159 had the right *endpoint* and the wrong *return type*.

### Data

```
calls
  id           TEXT PK      -- ULID, server-minted
  channel_id   TEXT NOT NULL
  started_by   TEXT NOT NULL   -- users.id
  started_at   TEXT NOT NULL
  ended_at     TEXT NULL       -- NULL = live
  invite_msg_id TEXT NULL      -- the signed invite that announced it, when there is one
  -- partial unique index: at most ONE live call per channel
  UNIQUE (channel_id) WHERE ended_at IS NULL
```

Room name: `room_for_call(call_id)` = the existing `gateway_id` namespacing applied to
`call:<ulid>`, mirroring `room_for_channel` so both live in one module.

That partial unique index is the whole glare fix (see *Degenerate states*), and it is
enforced **in the database**, not in a service check — the island runs SQLite with FK off
and application-level cascades, so a uniqueness invariant that must hold under concurrent
writers belongs in an index, not in Python.

### Endpoints

```
POST /v1/channels/{channel_id}/calls        -> 200 {call_id, room, started_at, joined: bool}
GET  /v1/channels/{channel_id}/call         -> 200 {live, call_id, participants, started_at}
POST /v1/calls/{call_id}/video-token        -> 200 {token, url, room, can_publish}
```

- **POST /calls is IDEMPOTENT-BY-LIVENESS**, and this is load-bearing: if a live call
  already exists for the channel it returns **that** call with `joined: true` rather than
  minting a second one. Two people tapping Call simultaneously converge on one room.
- **GET /call keeps #3159's contract exactly** — membership-enforced, existence-hiding
  **404 not 403**, **503** when video is unconfigured (identical to `video-token`, so the
  app keeps one code path for "this island has no video"), and **`participants` is a
  COUNT ONLY.** I agree with the app tab and am not tie-breaking: returning identities
  would make this a presence-probe for any channel member, and the count is strictly
  less information while being sufficient for the ring.
- **`video-token` MOVES, it does not change.** Every existing gate — `acl.readable_channel`
  existence-hiding, `is_blocked_between`, `acl.is_posting_member` → `can_publish`,
  rate-limit, `Cache-Control: no-store`, 503-when-unconfigured — is re-applied against
  the call's **originating channel**. Nothing about the trust boundary is redesigned; the
  lookup gains one hop (`call_id → channel_id`).

### Which identifier the client receives (explicit, because we just got bitten)

The app tab lost time to `channel_id.startsWith('dm:')` — justified by our real
`ck_channels_dm_prefix` CHECK, which is **on `channels.aiko_channel`, a column the client
never receives**. So, stated as contract:

| Concept | Client receives | Never sent to the client |
|---|---|---|
| channel | `channel_id` — bare ULID | `channels.aiko_channel` (`dm:{lo}:{hi}`) |
| call | `call_id` — bare ULID | — |
| LiveKit room | `room` — opaque, from the token response, **used verbatim** | the derivation rule |

**The client must never construct or parse a room name.** It is an opaque string handed
back with the token. No `startsWith("call:")` gate is ever correct.

### Liveness: webhook fast-path, reconcile at the decision point

LiveKit webhooks (`room_started`, `room_finished`, `participant_joined`,
`participant_left`) let the island maintain `calls.ended_at` **without ever calling the
SFU** — which matters because the island has *never* made an outbound SFU call and
#3159's 1/s poll would have introduced one on the ring path.

But LiveKit states **"no guarantees around delivery."** So webhooks are a **fast path,
never the authority**:

- The **poll** (`GET /call`, ~1/s during a ring) is served from island state — cheap, no
  outbound call.
- The **on-answer check** — the one decision that actually matters — additionally
  reconciles against `ListParticipants` **once**, and that is the only outbound SFU call
  in the design.
- A **wall-clock TTL** closes any call whose last webhook is older than `N`, so a dropped
  `participant_left` cannot mark a call live forever.

`ended_at` is therefore set by *whichever comes first*: a webhook, a reconcile, or the
TTL sweep. Three writers, one column, all monotonic (live → ended, never back).

## Build order (each step independently useful)

1. **`calls` table + `room_for_call` + POST/GET, no webhooks.** Liveness = "a call row
   exists and is younger than the TTL". Crude, but it already fixes concurrent-call
   collision and gives the app a `call_id`. Ships without touching the SFU at all.
2. **`POST /v1/calls/{id}/video-token`** — move the existing gates onto the call. The old
   channel-scoped route stays, serving v1 clients, until the app has migrated.
3. **Webhook receiver** (`POST /v1/livekit/webhook`, JWT+sha256-verified) → truthful
   `live` / `participants` / `ended_at`.
4. **WS `call.ended` frame** → deletes the poll. Safe to leave unsigned: per the app
   tab's asymmetry, forging a *start* triggers a camera (must be signed), forging a
   *stop* only suppresses a ring (a nuisance a hostile island could cause anyway by
   dropping the message).
5. Retire the channel-scoped `video-token` once the app has moved.

Steps 1–2 are the increment. 3–5 are follow-ons that each remove a wart.

## Blast radius & consent spine

- **New inbound public surface** (the webhook) on a live island. Must be
  JWT+payload-hash verified with the existing API secret, rate-limited, and fail-closed
  on an unverifiable signature. An unauthenticated webhook is a remote "end anyone's
  call" primitive.
- **Shared HS256 secret across islands** (#2732) means room-name unguessability is **not**
  a security property. Authorization comes from the island's own membership+block check,
  never from knowing the room name. Nothing in this design may rely on a name being
  secret.
- **Fail-open preserved.** The app rings anyway on 404/501/timeout. Only one island may
  have this for a while, so the absence of these endpoints must never make the app
  silent. Nothing here changes the ring-start path.
- **DM-only stays for now.** Per-call consent is what eventually *lets* group calls in,
  but that is #2731's selective-subscription work, not this increment.

## Claims to falsify (for the Temper — strike these hardest)

1. **"The callee can identify the right call without it being in the invite body."** The
   binding is channel + time, not cryptography. A stale invite could resolve to a
   *different* live call. Mitigation proposed: admit only if `call.started_at >=
   invite.signedAtMs - skew`, leaning on the app's existing 10s freshness gate. **Is that
   sufficient, or does the call id have to be in the signed body after all?**
2. **"The partial unique index solves glare."** Two simultaneous POSTs under SQLite —
   does one cleanly lose and read the winner's row, or can both see no live call and race?
3. **"Webhooks + TTL + one reconcile is sound."** The failure mode of a mis-set TTL is a
   ring that will not stop — exactly the bug we are fixing.
4. **"Moving `video-token` changes no gate."** A moved gate is a rewritten gate until
   proven otherwise; the #1d7c lesson is that a fix for one invariant regresses another.
5. **"Zero wire-format change."** Is that true, or have I pushed the versioning problem
   into the *endpoint* set, which the app must feature-detect anyway?

## Rejected alternatives

- **#3159 as cast (occupancy on the eternal room).** Rejected: `since` is a heuristic
  reconstructing a missing identity, and it forces an outbound SFU call on a 1/s path.
- **Derived room name** (`call:H(signer‖channel‖clientMsgId)`) — the reframe from Ore.
  **Rejected, and it was my favourite.** It fails on **glare**: two people calling at once
  derive two *different* rooms, both ring, both answer their own, neither connects. A
  derived name is a pure function of the invite, so it structurally cannot dedupe — and
  dedupe is what makes a call an object. It also needs the #3167 composite (a bare
  client-chosen `clientMsgId` is attacker-steerable — `feedback_identity_as_mutable_key`).
  Server-minting earns its place by making uniqueness enforceable.
- **A new `MessageKind` or WS frame for the invite.** Already rejected app-side and
  correctly: `signingBytes` does not cover `kind`, so the one field that lights a camera
  would be the one forgeable field.

## Open variables (enumerated, not silently TODO'd)

- The liveness **TTL** value — depends on LiveKit's `departureTimeout` on our v1.13.5
  config, which this pass did **not** establish. Read it off the box; do not guess.
- Whether `GET /call` should return `call_id` when `live: false` (i.e. "the last call")
  — useful for missed-call rendering, but it widens what a non-participant member learns.
- Whether the webhook receiver lives on the island or the media companion stack.
