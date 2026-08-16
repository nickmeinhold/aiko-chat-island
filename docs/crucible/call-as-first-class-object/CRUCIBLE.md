# Crucible — a call as a first-class object

*Movement 1 (Ore) — target pre-selected by Nick (claude-tasks #3170), consent gate
crossed by explicit direction ("go", 2026-08-16 10:01). This file is the enthusiasm
case + the falsifier the Temper must strike.*

## The pick

Today **the LiveKit room IS the channel id** (island handoff #2726, implemented in
`domain/livekit_tokens.py::room_for_channel`). A DM therefore has exactly **one room,
forever**, and every call two people ever have is that same room — distinguished only
by who happens to be standing in it at the moment you look.

The proposal (#3170): **mint a room per CALL**, and make a call a first-class object
with identity, a lifecycle, and a history.

## Why this thrills me — AND what it changes

The heat is that **#3159 contains the proof that #3170 is right, and neither tab
noticed.** #3159 asks for an occupancy endpoint returning `{live, participants, since}`
— and `since` exists, in its own words, *"so a callee can tell 'still the call I was
rung for' from 'a different call started in the meantime'."*

That field is a **timestamp heuristic reconstructing an identity that does not exist**.
It is the shape of a workaround, and it is in the API we were about to ship. #3170 names
the root cause one paragraph later: *"occupancy of a permanent room cannot distinguish
'the call I was invited to' from 'a different call that started since' from 'nobody has
ever called here'."*

What it changes, concretely — each of these is currently patched around, and all four
are downstream of the same missing object:

1. **The hangup-before-answer case gets a truthful answer** instead of app-side
   heuristics. (Caller bails at 3s; callee still rings, answers, lands alone.)
2. **Call history / missed calls become expressible** — there is something to attach a
   duration and a participant list to. (App tab #3164 wants to render invites as call
   events; today they can only infer from a message body.)
3. **Concurrent calls in one conversation stop colliding** into one room.
4. **Per-call consent replaces a DM-only gate.** The app had to build a DM-only
   `admitRing` because the only thing distinguishing "a call" from "a conversation" is
   `channels.kind` — a human typing the sentinel in `#general` would otherwise ring every
   member at once. The island has the mirror of this hack: `video-token` is **DM-ONLY by
   cage-match ruling (#122 rd7)**, because a room-level token cannot enforce pairwise
   blocks. Both halves are working around "the room is the conversation."

## The spark — the reframe I most want struck

#3170 prices itself as requiring a **v2 invite wire format**: if the room is
`call:<ulid>` minted by the server, the invite body must carry it, and
`kCallInviteBody` is a **one-way door** already in signed history on enspyr.

But the invite **already has a signed, unique, content-bound identity.** From the app's
design of record (`call_invite.dart`): `CallInvite.inviteId` is the `clientMsgId` out of
the origin envelope — *"two deliveries of ONE invitation share it; two genuine
invitations never do"* — and `signingBytes` covers `clientMsgId` and `channelId`.

**So if the room name is DERIVED from the signed invite rather than MINTED by the
server, we may get every benefit above while the body keeps carrying zero parameters and
`kCallInviteBody` never needs a v2.** Both parties already hold the identity. It is
already inside the signature. Nobody has to open the one-way door.

*Oh, of course*: the call id was never missing — it was sitting unread inside the
envelope we already sign.

## The falsifier (what would prove this ore is slag)

**If the derived-room shape cannot be made safe, the reframe is slag** and we fall back
to server-minting (which is #3170 as cast, and still better than #3159). It dies if any
of these cannot be closed:

- **Replay.** An old invite is re-delivered (reconnect drain, scrollback, a hostile
  island re-sending). Derivation is deterministic, so a replayed invite names a *valid*
  room. The 10s freshness gate is app-side and advisory — the ISLAND must not mint a
  token for a call that is over, or replay becomes a re-entry primitive.
- **The authorization inversion — the sharpest one.** Today the island *chooses* the
  room name, so a token is only ever signed for a room the island named. Under
  derivation the **client** presents a room name and the island must decide whether to
  authorize it. That inverts the trust direction on a live capability surface. The
  island must be able to independently recompute the name from a message it has stored,
  or it is signing tokens for attacker-chosen room strings.
- **Group calls.** With no single inviter there is no single signed invite to derive
  from. Does the shape degrade, or does it simply not extend?
- **Two devices, one user.** Both derive the same room (good) — but LiveKit identity is
  `user.id`, and two participants with one identity is already a known collision
  (#2730).
- **A retracted or taken-down invite.** The invite is a message; messages can be
  retracted (#7 forward-ULID retraction) and taken down. If the room is derived from a
  message that no longer exists, what authorizes the token?
- **Empty-room semantics.** LiveKit auto-creates a room on join and reaps it when empty.
  A per-call room that is reaped and then re-entered by a late joiner may silently
  resurrect a "finished" call.

## The constraint the ore must survive (verified 2026-08-16, not assumed)

**The island has NEVER talked to the LiveKit server API.** `grep` for
`ListParticipants|RoomService|LiveKitAPI` across `src/` returns **nothing**. The island
is purely a JWT *authorizer*: `mint_room_token` signs a `video` grant with the shared
secret and explicitly withholds `roomCreate`/`roomAdmin`/`roomList`
(`domain/livekit_tokens.py`).

This is load-bearing for BOTH proposals and neither issue mentions it:

- **#3159's occupancy endpoint requires `RoomService.ListParticipants`** — a brand-new
  **outbound** dependency island → SFU, on a path the app polls **~1/s during a 30s
  ring**. New failure mode (SFU slow/down → the endpoint hangs or 5xxs), new latency,
  and it needs a *higher-privilege* credential than anything the island signs today.
- A per-call object with a real lifecycle may be able to answer "is this call live"
  **from island state** (participants join/leave via webhook, or the object is closed
  explicitly) — i.e. **the object model may REMOVE the need for the outbound call that
  the occupancy endpoint forces.** If so, #3170 is not merely better-shaped than #3159,
  it is *cheaper at the trust boundary*, which inverts the usual "the object model is
  over-build" objection.

Also live and unmentioned: all islands share **one HS256 LiveKit secret** (#2732), so
`gateway_id` room namespacing prevents accidental collision but is **not** a
cryptographic tenant boundary. Any room-naming decision inherits that.

## Verified-real substrate (not invented)

- `src/aiko_gateway/rest/livekit.py` (162 lines) — `POST /v1/channels/{id}/video-token`,
  DM-only, existence-hiding 404, block-gated, `can_publish` from `acl.is_posting_member`,
  503 when unconfigured, rate-limited, `Cache-Control: no-store`.
- `src/aiko_gateway/domain/livekit_tokens.py` — `room_for_channel`, `mint_room_token`,
  `gateway_id` namespacing, fail-closed on blank room/identity.
- `../aiko_chat_app/lib/features/call/domain/call_invite.dart` @ `4ed99e4` (branch
  `feat/call-ring`) — the app-side design of record.
- claude-tasks #3159, #3170, #3167, #3164, #3163, #3165; #2726, #2728, #2730, #2731,
  #2732.
- Both islands carry TURN on `turns:443` (probed 2026-08-16 10:00, `Verify return code:
  0` both). #3159's grounding note saying imagineering cannot carry a call is **stale**.

## Scope boundary

This forge decides the **call-object shape and its wire contract**. It does NOT decide
group-call consent policy (#2731 selective subscription), multi-device identity (#2730),
or per-island LiveKit keys (#2732) — but the design must not foreclose them.
