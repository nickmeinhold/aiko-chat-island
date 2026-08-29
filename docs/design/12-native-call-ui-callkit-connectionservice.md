# Design 12 — Native call UI (CallKit / ConnectionService) and what it costs the island

**Status:** PROPOSED. Nothing built, nothing agreed. Written 2026-08-29 the day the
platform choice was made, so the reasoning exists before the build rather than after.
**Not yet grounded against the app tab's reply** — the questions put to it are in
§Open questions, and its answers outrank anything asserted here about the client.

**Decision of record it rests on:** Nick, 2026-08-29 — the client will use **CallKit +
PushKit on iOS** and **ConnectionService + full-screen intent on Android**. That is a
client decision; this note records only what it forces on the *gateway*.

**Tier:** v0.11.0 (structural). It changes the schema, changes the wire, and re-prices a
trust boundary — so by the working conventions it is cage-match by law, the wire half is
agreed with `aiko_chat_app` before either side merges, and **the island deploys first**.

---

## The one-sentence version

Native call UI needs a *call* — an addressable object with an identity, a liveness
state, and a cancel signal. The island has never had one: a ring is a direct message
whose body happens to equal a pinned sentinel string, and the push that carries it
names a channel, not a call. Everything expensive below follows from that single gap.

## Where the island actually is today

Read from source, not recalled:

- **A ring is a message.** `push_service.CALL_INVITE_BODY` is the pinned sentinel
  `"aiko:call/1 · 📞 started a call"`, compared with an exact match (never a prefix —
  a prefix test hands an attacker a wake primitive with arbitrary trailing content).
  `should_wake(channel_kind, body)` is true iff the channel is a DM **and** the body is
  exactly those bytes.
- **The push names a channel, not a call.** `_payload` is deliberately opaque and carries
  a single custom field, `c` = channel_id. The docstring is explicit that naming the
  caller is the thing being refused, because APNs can read everything we send it.
- **It is an alert push, not a VoIP one.** `apns-push-type: alert`,
  `apns-priority: 10`, `apns-expiration` = `_EXPIRATION_SECONDS` = 60, and **no retry**,
  deliberately (a ring that surfaces late is worse than no ring).
- **One token per device.** `device_tokens` has `UNIQUE(token)`, `platform`
  (`Platform.APNS` | `Platform.FCM`), and `apns_environment` (#3386). Nothing records
  what a token is *for*.
- **Android is unbuilt.** `Platform.FCM` exists in the enum; `config.py` says plainly
  that "Android/FCM is a separate transport behind the same door, NOT built yet".
- **There is no call-end concept at all.** `CALL_INVITE_BODY` is the only sentinel the
  island knows. A call-end message is an ordinary DM the island persists and fans out,
  and it wakes nothing, because `should_wake` requires the invite bytes.

## The thing that changes everything: the failure inverts

Today a stale invite is a **silent non-event**. That is the live bug (2026-08-29, with
the app tab): a push-woken invite can only arrive via REST history — it is `<= fence` by
construction, so the island never emits it as a live frame — and its `created_at` is
stamped at island-receive, so its measured age necessarily includes APNs delivery, human
reaction, cold start, handshake and backfill. The client's 10s freshness window cannot
admit the exact case push exists to serve. The user sees nothing.

Under CallKit the *same* stale invite is a **full-screen ring, through silent mode and
Do Not Disturb, for a call nobody is on.**

Same missing predicate, opposite sign. This is the strongest available evidence that the
real work is a **call-liveness predicate**, not a tuned constant — and native UI attaches
a deadline to it, because the new failure is one every user sees and none can explain.

---

## Decision 1 — a call becomes an addressable object, and it lands FIRST

CallKit and ConnectionService are both built around a **per-call UUID**. Cancellation
needs to address one specific call. With only `c` (channel_id) the island can say
"stop ringing for channel X", which is wrong under any concurrency and racy whenever a
cancel overtakes its invite.

So: **a call id is minted at invite time and carried by every push about that call** —
the invite, the cancel, and any later state signal. Both platform APIs, the cancel path,
and the client's liveness predicate are four things blocked on this one object, so it is
the first increment and nothing else starts before it.

This is deliberately the *smallest* version of "call as a first-class object" that the
platform forces: an identity and a lifecycle signal, not a participant set, not a call
table with policy on it. §Open questions Q1 asks the app tab whether that is enough.

**Cross-repo note, surfaced not resolved.** The `call as first-class object` crucible
converged CANDIDATE INVALIDATED (3/4 DISSOLVE, 2026-08-16); #3170 and #3172 were closed
and the product fix moved to a signed call-end sentinel in the app repo (#3198). That
dissolve was argued on a **pre-CallKit premise**. The platform now demands a call
identity and a cancel signal regardless of what either repo would prefer. That is a
changed premise, not a reversal — and per the working conventions the app tab's record
is the other half of the binding contract, so this note raises the conflict and does not
tie-break it.

## Decision 2 — two token kinds per device, and the partial state is the risk

PushKit issues a **VoIP token that is distinct from the APNs alert token**. So
`device_tokens` gains a `token_kind` (`alert` | `voip`), NOT NULL with a server_default
of `alert` so a direct INSERT that omits it gets the value every existing row already
means — the same shape as `apns_environment` in #3386, and for the same reason.

The column is the easy half. The risk is the **partial state**:

- registration is an UPSERT KEYED ON THE TOKEN that reassigns `user_id`, because a device
  changes hands (logout A → login B on the same phone) and a push must reach the current
  owner;
- with two tokens per install, a handover must reassign **both**;
- a device holding one but not the other is **half-reachable** — fine for messages,
  unreachable for calls, or the reverse — and today's `/health`
  `push.devices_unreachable` signal cannot express that.

An island that holds devices it cannot reach already says so (#3397). It should say
*which capability* it cannot reach them for, or the signal quietly becomes a lie the day
the second token kind exists.

## Decision 3 — one credential, two topics (this part is cheap, and the preflight moves with it)

VoIP needs `apns-topic: <bundle-id>.voip` and `apns-push-type: voip`. Because the island
authenticates with a **`.p8` token (JWT)** rather than a certificate, **the same signing
key covers VoIP** — there is no second credential set to provision, rotate or leak. The
config gains one field (the VoIP topic); `apns_key_id`, `apns_team_id` and
`apns_private_key` are untouched.

Two consequences that must not drift apart:

1. The half-configured guard is **all-or-none** by design, and it now has a fifth member.
   A partial set refuses to boot — correct, and must not be weakened.
2. **`deploy/preflight-apns.sh` must gain the same member in the same change.** It shipped
   in v0.9.1 (live on both islands 2026-08-29) precisely so a partial set aborts the
   deploy while the operator still has a running island. A preflight that checks four of
   five keys passes a config that cannot ring — a check that cannot detect the failure it
   exists for. Its tests (`tests/test_deploy_preflight.py`) exercise all/none/partial and
   must be extended in lockstep.

## Decision 4 — the send path forks at the door, and VoIP is calls only

Apple's contract, which has no slack in it: since iOS 13 **every VoIP push must be
reported to CallKit immediately** on receipt. Miss it and the app is terminated; do it
repeatedly and VoIP push privileges are revoked. Apple's policy is likewise that VoIP
pushes carry calls and nothing else.

So the island can never send a VoIP push that *might* not be a call, and the alert/VoIP
decision is made **at the island, correctly, every time**:

```
is this a call?  -> voip token  + <bundle>.voip topic + push-type voip
otherwise        -> alert token + <bundle> topic      + push-type alert
```

`should_wake` already holds the call predicate, so the fork belongs beside it, inside the
one door every send path passes — the same discipline as `should_federate`. A second send
path must not be able to forget which kind of push it is making.

The deeper consequence: **there is no on-device window in which to reconsider.** The
client cannot receive a VoIP push, check whether the call is still live, and decline to
ring — it must ring first. Send-time correctness therefore moves onto the island, at
exactly the point where it has no call model. Decision 1 is what makes Decision 4
implementable.

## Decision 5 — cancellation is a first-class push, with its own gates

A cancel is not an ordinary message that happens to be fanned out. It must:

- **reach a device that may never have received the invite** (the invite push expired,
  was dropped — there is no retry — or the device was off);
- **be idempotent and ordered against its invite**, since a cancel can overtake it;
- **carry the call id from Decision 1**, never just a channel;
- **pass its own admission gate.** `should_wake` is written for the invite bytes; a cancel
  is a second wake reason and inherits none of that reasoning for free.

A cancel that cannot be addressed, or that arrives for a call the device never learned
about, is the mechanism by which the inverted failure becomes permanent — a phone ringing
with nothing able to stop it.

## Decision 6 — the opaque payload survives, via placeholder-then-update

`_payload` refuses to name the caller: a payload saying "Alice is calling you" tells Apple
who calls whom, on a product whose thesis is that such facts stay with the operator. The
accepted cost, stated honestly in the code, is that the lock-screen banner reads
"Incoming call". CallKit raises the stakes because its full-screen UI **wants a caller
name**.

Three options, and the recommendation is the middle one:

| | keeps the thesis | cost |
|---|---|---|
| put the handle in the VoIP payload | **no** | tells Apple who calls whom |
| **report placeholder, then `reportCall(with:updated:)` after fetching from the island** | **yes** | a round-trip inside the PushKit handler |
| show the island name only ("Aiko call") | yes | worst UX, no caller ever named |

The middle path satisfies report-immediately *and* keeps the identity off Apple's wire,
by making the anonymous window short rather than by filling it. It is the same mechanism
as the Notification Service Extension previously scoped out for the banner — one
mechanism serving both surfaces.

**This is a client-side viability question inside a window the island cannot see**, so it
is Q2 to the app tab, not a decision this note gets to make. What the island owes it is
an endpoint that resolves a call id to a display identity for an authorised member,
fast — and, notably, *nothing new on the wire to Apple*.

## Decision 7 — re-price the wake gate at the new blast radius

The existing gates — DM-only, exact sentinel, not-a-blocked-pair — were priced against a
**banner**. One accepted risk is recorded explicitly in `push_service`: a muted DM still
wakes the handset, because the mute is client state and you cannot un-ring a phone.

Under CallKit that same wake is a full-screen ring that punches through silent and Do Not
Disturb. **A hole in `should_wake` stops being a spam vector and becomes a remote
full-screen-ring primitive against an arbitrary user** — a harassment tool, not a
nuisance. The accepted risk was priced at the old radius and does not carry forward by
default; it gets re-argued at the cage-match, with mute re-examined now that the cost of
honouring it has changed sign.

## Decision 8 — Android is a new transport, not a modification

`Platform.FCM` exists in the enum and nothing implements it. ConnectionService therefore
means building the FCM send path from zero: credentials, send, the token lifecycle, the
reaper's equivalent of APNs' 410/`BadDeviceToken` handling, and the same alert/VoIP-shaped
fork (FCM high-priority data message for a call, notification message otherwise).

**Confidence marker, stated rather than buried:** Android 14+ gates
`USE_FULL_SCREEN_INTENT` — it is no longer freely granted, non-calling apps must request
it, and Play reviews the declaration. A calling app should qualify. My confidence on the
*current* gating and review criteria is moderate, and it is policy that moves; it must be
checked against current documentation by whoever owns the Android half rather than taken
from this note. Failure there is silent — the notification simply does not go full-screen.

---

## What gets better

`_EXPIRATION_SECONDS = 60` (island) and `kCallInviteFreshness = 10s` (app) are two answers
to one question — *how late is too late for a ring?* — decided in different repos, 6×
apart, each with a written justification, neither aware of the other. The gap between them
is where the current bug lives.

Native call UI **forces the reconciliation**, because a VoIP push should expire exactly
when the ring stops. The two constants collapse into one number with a physical meaning
(ring duration), derived from call semantics rather than from wire latency. That is a
genuine simplification arriving with the cost.

## Sequencing

1. **Call identity** (Decision 1) — schema + wire. Everything else is blocked on it.
2. **Token kind + half-reachable signal** (Decision 2) — schema, and the `/health` change.
3. **Topic + preflight** (Decision 3) — config, moving in ONE change with its own test.
4. **Send-path fork** (Decision 4) and **cancel push** (Decision 5) — the trust-boundary
   work, cage-matched together because they share a gate.
5. **Identity-resolution endpoint** (Decision 6) — gated on the app tab's answer to Q2.
6. **FCM transport** (Decision 8) — independent of 1-5 once the call object exists.

Decisions 4, 5 and 7 are one cage-match, not three: they are the same trust boundary seen
from three sides, and reviewing them apart is how a fix for one re-opens another.

## Open questions — for the app tab, sent 2026-08-29

- **Q1.** Does the crucible's DISSOLVE of call-as-first-class-object survive the CallKit
  premise, or does it need re-opening? Decision 1 assumes the *minimum* object (identity +
  lifecycle signal) and no more. The app tab owns that record.
- **Q2.** Is placeholder-then-`reportCall(with:updated:)` viable inside the window PushKit
  allows? Decision 6 depends on it; if not, the privacy decision itself needs re-opening
  with Nick.
- **Q3.** Can the client's registration path ever land one token kind without the other? If
  yes, the half-reachable state in Decision 2 is real and gets modelled rather than
  discovered in production.
- **Q4.** How is #3588's notification-tap handler scoped now? Under CallKit the ring no
  longer arrives via a tap; the handler stays correct for ordinary message pushes but
  stops being the ring's entry point.

## Provenance

Written by Claude (island tab) 2026-08-29, from a decision Nick made the same day, and
grounded in `push_service.py`, `apns.py`, `models.py`, `config.py` and the live 2026-08-29
cross-tab diagnosis with `aiko_chat_app`. Claims about the client are the island's *reading*
of the client and are marked as questions where they matter. High confidence on the PushKit
report-immediately contract, the distinct VoIP token, and `.p8` auth covering VoIP with only
a topic change; explicitly thinner on current Android full-screen-intent policy (Decision 8).
