# Heat — research (2026-08-16)

> **Depth label: BOUNDED (shallow).** Run as an inline research pass, not `/deep-research`
> and not a background researcher (this session carries a standing instruction not to
> spawn agents unrequested). Vendor claims below are quoted from LiveKit's own docs and
> cited; repo claims are grepped, not recalled. **Anything marked UNVERIFIED must be
> confirmed against the running SFU (v1.13.5) before it becomes load-bearing.**

## Finding 1 — the island has never called the LiveKit server API (VERIFIED, repo)

```
grep -rn "ListParticipants|RoomService|room_service|api.LiveKitAPI" src/ --include="*.py"
→ (no matches)
```

The island is a pure JWT **authorizer**. `domain/livekit_tokens.py::mint_room_token`
signs a `video` grant and *deliberately withholds* `roomCreate` / `roomAdmin` /
`roomList`. `rest/livekit.py` (162 lines) is the only video door.

**Consequence, and it reframes the whole fork:** #3159's occupancy endpoint would
introduce the island's **first outbound dependency on the SFU** — on a path the app polls
**~1/s for a 30s ring**. New failure mode (SFU slow → the ring endpoint hangs), new
latency, and it needs a higher-privilege credential than the island signs today. Neither
#3159 nor #3170 mentions this.

## Finding 2 — LiveKit emits webhooks; liveness need not be polled (VERIFIED, vendor)

Events (exact names): `room_started`, `room_finished`, `participant_joined`,
`participant_left`, `participant_connection_aborted`, `track_published`,
`track_unpublished`, plus egress/ingress events. All carry `id`, `createdAt`, `event`.

Authenticated by an `Authorization` header carrying **a signed JWT whose payload includes
a sha256 hash of the body** — so the island can verify authenticity *and* integrity with
the API secret it already holds.

**This is the most important finding in the pass.** It means "is this call live" can be
answered **from island state**, with no outbound call to the SFU at all. The object model
does not merely organise the data better — it can make the liveness question *cheaper at
the trust boundary* than the occupancy endpoint that was supposed to be the lighter
option. It also supplies a natural trigger for a WS `call.ended` frame, which deletes the
1/s poll entirely.

### Finding 2a — but delivery is BEST-EFFORT (VERIFIED, vendor — the trap)

LiveKit states there are **"no guarantees around delivery."** It retries transient
failures and preserves ordering ("only delivering newer events after older ones have been
delivered or abandoned") — but **abandoned is a real terminal state.**

So island-held call state **can go stale in the dangerous direction**: a dropped
`participant_left` / `room_finished` leaves a call marked `live` **forever**, and the
callee's ring never stops — reintroducing the exact bug #3159 exists to fix, now with a
confident-looking API in front of it.

**Any design that treats webhooks as authoritative without a reconciliation path is
unsound.** Mitigations to weigh in Cast: a wall-clock TTL on `live`, a lazy
`ListParticipants` reconcile *only* on the on-answer check (not the poll), or treating
island state as a fast-path hint with a bounded staleness contract.

## Finding 3 — rooms auto-create on first join (VERIFIED, vendor)

*"A room can be created manually via server API, or automatically, when the first
participant joins it."* So a per-call room needs **no** `roomCreate` grant and no server
API call — a token naming `call:<id>` is sufficient to bring the room into existence.

This is what makes a derived-room shape mechanically possible at all.

## Finding 4 — room lifetime knobs (VERIFIED name/meaning, defaults UNVERIFIED)

- `emptyTimeout` — seconds to keep the room open **before any participant joins**.
- `departureTimeout` — seconds to keep the room open **after the last participant
  leaves**, an explicit rejoin grace period.

Numeric defaults were **not** established by this pass (sources disagree / are
version-specific; one doc example uses 20s illustratively). **Do not encode a default in
the design** — read it off the running v1.13.5 config, or set it explicitly.

Relevant because a per-call room that is reaped and then re-entered by a late joiner will
**auto-create a fresh room under the same name**, silently resurrecting a "finished"
call. A call object with an explicit `ended_at` is what closes that.

## Finding 5 — one shared HS256 secret across islands (VERIFIED, repo comment)

`livekit_tokens.py` records that `gateway_id` namespacing prevents *accidental*
room/identity collision but is **"NOT a cryptographic tenant boundary"** — all islands
share one SFU secret (#2732). Any room-naming scheme inherits this: a second island
holding the same secret can mint a token for our room whatever we name it.

**So room-name unguessability is not a security property.** Authorization must come from
the island's own check, never from the name being hard to guess. This kills any variant
of "derive a secret-ish room name and treat knowing it as proof of invitation."

## Finding 6 — the invite already carries a signed identity (VERIFIED, app repo)

`call_invite.dart` @ `4ed99e4`: `CallInvite.inviteId` is the `clientMsgId` from the origin
envelope — *"two deliveries of ONE invitation share it; two genuine invitations never
do."* `signingBytes` covers `domainTag ‖ pubkey ‖ channelId ‖ clientMsgId ‖ signedAtMs ‖
body ‖ replyTo`, and **not** `kind`, and **not** `sender`.

App-tab #3167 is already moving to a **composite** identity (signer + channel +
clientMsgId) because a naked `clientMsgId` is client-chosen and therefore not unique
across signers.

**Consequence for the derived-room reframe:** any derivation MUST use the composite. A
room derived from a bare client-chosen `clientMsgId` is attacker-steerable — two signers
can pick the same value and collide into one room. This is
`feedback_identity_as_mutable_key` almost verbatim.

## Open questions carried into Cast

1. Can the island **independently recompute** the room name from a stored message? (If
   not, the client is naming rooms and the island is signing whatever it is handed.)
2. What authorizes a token when the originating invite has been **retracted/taken down**?
3. Does the shape extend to **group calls** (no single inviter), or does it only not
   foreclose them?
4. Webhook endpoint = a new **inbound** public surface. Is inbound-authenticated better
   or worse than #3159's outbound dependency? (Different failure directions: inbound can
   be spammed; outbound can hang.)

## Sources

- [LiveKit — Webhooks](https://docs.livekit.io/home/server/webhooks/)
- [LiveKit — Room management](https://docs.livekit.io/intro/basics/rooms-participants-tracks/rooms/)
- [LiveKit JS Server SDK — CreateOptions](https://docs.livekit.io/reference/server-sdk-js/interfaces/CreateOptions.html)
- [livekit/livekit#1776 — empty room timeout adjustment](https://github.com/livekit/livekit/issues/1776)
- [livekit/node-sdks#126 — emptyTimeout docs](https://github.com/livekit/node-sdks/issues/126)
