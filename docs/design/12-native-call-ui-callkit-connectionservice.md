# Design 12 — Native call UI (CallKit / ConnectionService) and what it costs the island

**Status:** PROPOSED. Nothing built. **Revised 2026-08-29 after the app tab's answers**,
which reversed two of the first draft's decisions and deleted a third — the revision is
recorded in §What the first draft got wrong rather than quietly folded in.

**Decision of record it rests on:** Nick, 2026-08-29 — the client will use **CallKit +
PushKit on iOS** and **ConnectionService + full-screen intent on Android**.

**Tier:** v0.11.0 (structural). Schema change, wire change, and a re-priced trust
boundary — cage-match by law, wire half agreed with `aiko_chat_app` before either merges,
island deploys first.

---

## The one-sentence version

Native call UI needs an addressable call with a liveness signal. The island has never had
one — a ring is a direct message whose body equals a pinned sentinel, and the push names a
channel, not a call. **But the id does not have to be the island's**, and that single
correction is what takes this from "build a call object" to two small changes.

## What the first draft got wrong

Recorded because the reasoning matters more than the conclusion.

1. **It framed the call-as-first-class-object DISSOLVE as needing *re-opening* on a
   changed premise. Wrong — it needed reading correctly.** The strike killed a *decorative
   server row*: objecthood kept as a word, not a property; a v2 call that "could not decide
   anything, survive its own end, federate, or separate call N from N+1 at the SFU". It
   never ruled against call identity as such. CallKit wants precisely what that design
   refused to build — an id actually on the invite — so nothing needs re-litigating.
2. **It proposed the island mint the call id.** Unnecessary. A client-minted ULID in the
   signed invite body satisfies every constraint at a fraction of the cost (Decision 1).
3. **It proposed an island identity-resolution endpoint for the CallKit UI.** Deleted —
   it cannot work. On a push-woken cold start the CallKit report happens in Swift *before
   the Flutter engine exists*, so no fetch, and no Dart cache read, can precede it
   (Decision 6).

## Where the island actually is today

Read from source:

- **A ring is a message.** `push_service.CALL_INVITE_BODY` is the pinned sentinel
  `"aiko:call/1 · 📞 started a call"`, matched exactly (never a prefix — that would hand
  an attacker a wake primitive with arbitrary trailing content). `should_wake` is true iff
  the channel is a DM **and** the body is exactly those bytes.
- **The push names a channel, not a call** — `_payload` carries one custom field, `c`.
  Deliberately opaque: naming the caller would tell Apple who calls whom.
- **Alert push, not VoIP:** `apns-push-type: alert`, priority 10, `apns-expiration` = 60s,
  and no retry, deliberately.
- **One token per device.** `device_tokens`: `UNIQUE(token)`, `platform`, `apns_environment`
  (#3386). Nothing records what a token is *for*.
- **Android unbuilt.** `Platform.FCM` exists; `config.py` says FCM is "a separate transport
  behind the same door, NOT built yet".
- **No call-end concept at all.** `CALL_INVITE_BODY` is the only sentinel the island knows,
  so a call-end message wakes nothing. **This is the real island blocker.**

## The thing that changes everything: the failure inverts

Today a stale invite is a **silent non-event** — the live bug (2026-08-29, with the app
tab): a push-woken invite is `<= fence` by construction so it can only arrive via REST
history, and `created_at` is stamped at island-receive, so its measured age necessarily
includes APNs delivery, human reaction, cold start, handshake and backfill. The client's
10s freshness window cannot admit the exact case push exists to serve.

Under CallKit the same stale invite is a **full-screen ring, through silent mode and Do Not
Disturb, for a call nobody is on.**

Same missing predicate, opposite sign. The work is a **call-liveness predicate**, not a
tuned constant — and native UI attaches a deadline to it.

---

## Decision 1 — the call id is CLIENT-minted; the island carries it, and owns no call object

The caller mints a ULID client-side and puts it in the signed invite body:

```
aiko:call/2 <ulid>          # invite wire v2 — the id is IN the signed body
```

The end sentinel references the same id. ULID is 128 bits, so it maps losslessly onto the
UUID CallKit and ConnectionService both require.

This satisfies every finding the temper left standing:

| finding | how a client-minted id satisfies it |
|---|---|
| **D5** — objecthood kept as a word, not a property | the id IS on the invite, which is exactly what v2 refused |
| **D1 (Tesla)** — a server liveness detector is blinded by its own trigger | there is no detector; cancellation is a **signed client message** |
| **D4 (Carnot)** — "a write you may skip and a read you must not trust is not an API" | no `POST /v1/calls`, no `GET`, no webhook, **no schema** |

**The island's entire half is therefore two changes:** carry the id in the push payload,
and learn to wake on the end sentinel. That is the whole cost of call identity.

**Wire v2 carries a permanent tail.** The v1 sentinel is already inside signed messages in
history on enspyr, so a **v1 read path exists forever** — v2 adds, never edits. The
sentinel was always a one-way door; this is the door being used, not bypassed.

**#3171 fires now.** Its trigger was recorded as not firing because "there is no island
call_id to carry". There still isn't — but there is now a *client* call_id to carry, which
satisfies the trigger by a different route than the one it was written for.

## Decision 1b — the sanctioned future island object, and why it is NOT this work

**Both tabs initially under-read the record here, so it is written down.** Issue #3170's
DISSOLVE set one reopening bar: *name a server-side decision that must be authoritative.*
Nick's ruling of 2026-08-17 states **that bar has been met** — under "calls are gatherings,
not channel properties", **per-call membership** is that decision, because who may join
*this gathering* can no longer be inherited from channel membership. The ruling says
explicitly that **Carnot's D4 objection does not apply to an ACL.**

So "the island may never hold call state" is **too strong**, and quoting D4 as a permanent
ban misreads it. What is true is narrower and load-bearing:

- an authoritative **per-call ACL** is sanctioned, and is a *new* design
  (gathering-with-its-own-ACL) rather than round 3 of the dissolved one;
- it **must not start before #3196 settles whether a gathering can span islands**, by that
  same ruling;
- **Decision 1 is deliberately not that design.** It is the minimum the platform forces,
  and it is forward-compatible with the ACL rather than a substitute for it.

## Decision 1c — CallKit weakens one of the DISSOLVE's own pillars (flagged, not acted on)

Maxwell's DISSOLVE pillar was *there was no server-only fact anyway*: "the shipped app
carries `kCallRingDuration = 30s`; a crashed caller rings for at most 30 seconds with no
island involvement."

That argument rests on a **Dart-side ceiling**. A CallKit ring is system UI drawn before
Dart exists and **does not self-expire** — it ends only when something calls
`reportCall(with:endedAt:reason:)`. So the 30s ceiling has to be re-established somewhere
that survives app suspension.

The app tab owns re-establishing it and has taken it. Recorded here because it means one
of the three DISSOLVE pillars is **weaker under CallKit than when it was cast** — which
does not reopen anything today, but is exactly the kind of premise-shift that should be on
the record before the gathering design is cast.

## Decision 2 — two token kinds, and PARTIAL IS THE DEFAULT

`device_tokens` gains `token_kind` (`alert` | `voip`), NOT NULL, `server_default='alert'`
so an existing row means what it already meant — same shape as `apns_environment` (#3386).

The first draft treated a half-registered device as an edge case. **The app tab's reading
of its own registration path says it is the normal case**, and one arm is permanent:

1. **Permission asymmetry — not a race.** The APNs alert token requires the user to grant
   notification permission. **A PushKit VoIP token requires no permission at all.** A user
   who declines notifications has a VoIP token and will *never* have an alert token.
2. **Two independent async callbacks** (`didRegisterForRemoteNotifications…` and
   `pushRegistry(_:didUpdate:for:)`); whichever lands first registers alone.
3. **Independent rotation** — either token can rotate without the other.
4. **Android has exactly one token.** FCM has no VoIP equivalent, so `token_kind` must not
   imply both-required *anywhere* in the model.

Therefore **"reachable for messages, unreachable for calls" is a legitimate state, not a
degraded one**, and `/health`'s `push.devices_unreachable` (#3397) must say *which
capability* it cannot reach a device for. An island that reports it holds devices it
cannot reach should not start lying the day there are two ways to be unreachable.

### Decision 2a — the worst state in the system

The app half maintains a durable unregister debt (`PendingUnregisterStore`) recording **a
set of tokens per island, with no kind**, and its correctness argument leans on the
island's upsert-on-token reassigning `user_id`.

Add a second token kind, and **a sign-out that discharges only one kind leaves a routable
VoIP row for a handset that has been signed out.** Today that mis-delivers a silent data
push. Under CallKit it is **a stranger's phone ringing full-screen, through silent mode,
for a call meant for the previous owner.**

The island must therefore **assume the debt can be partial** rather than assume pairing —
tear down by `(token)` as now, but never infer that discharging one kind discharged the
other. The app tab carries the client half.

## Decision 3 — one credential, two topics; the preflight moves in the same change

VoIP needs `apns-topic: <bundle-id>.voip` and `apns-push-type: voip`. Because the island
authenticates with a **`.p8` token (JWT)** rather than a certificate, **the same signing key
covers VoIP** — no second credential set to provision, rotate or leak. Config gains one
field.

Two things that must not drift apart:

1. The half-configured guard is all-or-none by design and gains a fifth member. A partial
   set refuses to boot — correct, and must not be weakened.
2. **`deploy/preflight-apns.sh` gains the same member in the same change.** It shipped in
   v0.9.1 (live both islands, 2026-08-29) so a partial set aborts the deploy while the
   operator still has a running island. A preflight checking four of five keys passes a
   config that cannot ring — a check blind to the failure it exists for.
   `tests/test_deploy_preflight.py` (all/none/partial arms) extends with it.

## Decision 4 — the send path forks at the door, and VoIP is calls only

Since iOS 13, **every VoIP push must be reported to CallKit before the delivery handler
returns**, or the system terminates the app; repeated violations revoke VoIP push
privileges. Apple's policy is likewise that VoIP pushes carry calls and nothing else.

```
is this a call?  -> voip token  + <bundle>.voip topic + push-type voip
otherwise        -> alert token + <bundle> topic      + push-type alert
```

`should_wake` already holds the call predicate, so the fork belongs beside it inside the
one door every send path passes — the same discipline as `should_federate`.

The deeper consequence: **there is no on-device window in which to reconsider.** The client
cannot receive a VoIP push, check liveness, and decline to ring — it must ring first. So
send-time correctness moves onto the island.

## Decision 5 — waking on the end sentinel is the island's real blocker

A cancel is not an ordinary message that happens to be fanned out. It must:

- **reach a device that never received the invite** (expired at 60s, dropped — there is no
  retry — or the device was off);
- **be idempotent and ordered against its invite**, since a cancel can overtake it;
- **carry the call id** from Decision 1, never just a channel;
- **pass its own admission gate** — `should_wake` is written for the invite bytes and a
  cancel inherits none of that reasoning for free;
- **remain a signed client message.** Per D1, the island never *infers* an end. It carries
  and wakes on one.

A cancel that cannot be addressed, or that arrives for a call the device never learned
about, is how the inverted failure becomes permanent: a phone ringing with nothing able to
stop it.

## Decision 6 — the opaque payload survives, and the island owes NOTHING for it

The first draft's island identity-resolution endpoint is **deleted**: on a push-woken cold
start the report happens in Swift before the Flutter engine exists, so nothing — network or
Dart cache — can precede it.

The app tab's exit is better and entirely client-side: **a small Swift-readable caller-name
cache** (App Group / `UserDefaults`, keyed by channel id) that Dart maintains as the roster
updates. The synchronous report reads it and already has the right name. No network, no
round-trip, and **nothing about who-calls-whom on Apple's wire** — `_payload` keeps its
refusal intact, unchanged.

Three tiers, degrading honestly: Swift-readable local cache → `reportCall(with:updated:)`
once Dart is up → **the placeholder string `Aiko`** (RULING: Nick, 2026-08-30). One word,
names the product not the person, and says nothing about who is calling — so the tier-3
fallback leaks no more than the payload already refuses to.

**Verified against Apple's documentation JSON** (the HTML site is a JS-rendered SPA that
returns title-only to both tabs' fetchers — a fact about the instrument, not the API):

```
func reportCall(with UUID: UUID, updated update: CXCallUpdate)
```

Tier 2 is real and does what it is being asked to do.

### Decision 6a — CallKit calls land in the system Recents list, and Apple does not document the default

Also verified from the same JSON:

```
var includesCallsInRecents: Bool { get set }
```

> "A Boolean value that indicates whether the provider includes a call in the system's
> Recents list after the call ends."

**Apple's own published documentation does not state its default** — the sentence in the
JSON is literally `"The default value of this property is ."`, truncated at source.

**RULING (Nick, 2026-08-30): appearing in Recents is fine.** A CallKit call landing in the
system call history is accepted, on a product whose thesis is that who-calls-whom stays
with the operator — the entry is local to the callee's own device and names a call they
were party to.

**It is still set EXPLICITLY**, to `true`. That is not hedging the ruling, it is the
engineering half of it: Apple does not publish the default, so an unset property means
"whatever this OS version happens to do" — the value would be a fact about the platform
rather than a decision of ours, and it could change under us in a point release without
anything failing. A ruling that says "fine" must still be written down somewhere the code
can be read from.

Residual, unverified and deliberately not assumed either way: whether iOS syncs that
history off-device. The ruling above is scoped to the on-device entry, which is what was
asked. If off-device sync turns out to be real it is a **new** question, not a settled
one.

## Decision 7 — re-price the wake gate at the new blast radius

The existing gates — DM-only, exact sentinel, not-a-blocked-pair — were priced against a
**banner**, including the accepted risk recorded in `push_service`: a muted DM still wakes
the handset, because mute is client state and you cannot un-ring a phone.

Under CallKit that wake is a full-screen ring punching through silent and DND. **A hole in
`should_wake` stops being a spam vector and becomes a remote full-screen-ring primitive
against an arbitrary user** — harassment, not nuisance. That accepted risk does not carry
forward by default; it is re-argued at the cage-match, with mute re-examined now that the
cost of honouring it has changed sign.

## Decision 8 — Android is a new transport, and its gate is store review

`Platform.FCM` exists and nothing implements it: credentials, send path, token lifecycle,
the equivalent of APNs' 410/`BadDeviceToken` reaping, and the same call/non-call fork.

The first draft flagged thin confidence on `USE_FULL_SCREEN_INTENT`. **The app tab checked
it and the shape is sharper than the odds:** it became a *special access permission* in
Android 14, auto-granted only to apps whose core function is calling or alarms; a **Play
Console declaration has been required since 31 May 2024**, with default-grant tightening
again from 22 Jan 2025; unapproved apps must prompt at runtime and degrade gracefully.

We plausibly qualify — this is genuinely a calling app. The finding is that **this is a
store-review dependency, not a code dependency, and it is slower than the build.** Whoever
owns the Play declaration should start it now rather than discover it at submission.

## Decision 9 — #3196 is a hard gate, and CallKit makes it louder

Cross-island calling is **broken today**: `room_for_channel()` is gateway_id-namespaced
(`imagineering:` vs `enspyr:`) and the two islands run separate SFUs. That was found during
the temper and filed as #3196, and it is the reason the gathering design is held.

Under CallKit the failure gets much louder: **a full-screen ring, through DND, for a call
the callee physically cannot join.** Today the same failure is a quiet dead end.

### RULING (Nick, 2026-08-30): the CALLEE's island hosts the call

This settles host-selection, which was the fork #3196 needed.

**Attribution, because it matters downstream:** the RULING is Nick's. The argument below is
**Claude's reconstruction**, not his stated reasoning — he confirmed 2026-08-31 that the call
was fast intuition and the justification came after. Recorded this way so a later reader can
re-examine the argument on its merits without believing they are overturning a ruling. If the
reasoning is wrong, the ruling may still be right.

**Why it looks like the right pick.** Under CallKit the callee's island *already owns the leg that
must work*: it holds the callee's device tokens and sends the wake. Hosting the room there
puts the push and the SFU on the same island, so the ring path carries **no cross-island
dependency at all**. The caller — awake, in the app, and acting — is the party made to
reach across.

**And it retires the hard part.** The caller's app connects to the callee's island's
LiveKit directly, over the public internet: an ordinary client→SFU connection. Both islands
already serve `turns:<domain>:443` publicly with real relay-only calls proven end to end.
So **there is no SFU-to-SFU federation, no clustering, and no new transport** — one call,
one SFU, chosen by a rule. That retires the open premise recorded against the media-topology
ruling ("self-hosted LiveKit may only cluster within one trust domain — verify before
designing"): under this ruling you never cluster, so the premise stops being load-bearing
rather than needing an answer.

**What remains is an AUTH problem, not a media one.** The callee's island must mint a room
token for **a user it does not own**, authenticated by the caller's home island. That is a
federation trust boundary — cage-match by law — and it rides machinery that already exists:
signed `/v1/island` manifests, `signing_keys`, and `origin` carriage. `room_for_channel()`'s
`gateway_id` namespacing stops being a bug and becomes the *host designation*, once the rule
says which gateway_id wins.

### Decision 9a — the caller's island FORWARDS the token request (island-to-island)

The app's `VideoToken.url` and `room` already come from the response, never from local
config ("the deployment owns its transport"), so **the client's connect path already accepts
a foreign SFU** and the ruling costs it nothing there. The seam is one layer up: the token
request is a *relative* path on the authed client, bound to the user's own island, so a
caller structurally cannot ask anywhere else.

Two shapes were possible; **the caller's island forwards on the caller's behalf**, chosen
here:

- **the client keeps its invariant** — the app only ever talks to its own island, and never
  has to weaken that anywhere else in the codebase;
- **no foreign credential lands on the device.** The alternative (client talks to the callee's
  island directly) still needs a home-island assertion to present, so it is this design plus
  an extra hop, with a bearer artifact on a handset and a harder revocation story;
- **the trust edge is island↔island, where the machinery already exists** — signed
  `/v1/island` manifests, `/v1/keys`, `signing_keys`. The caller's island already
  authenticates the caller; it vouches, it does not re-prove.

So the island owes an endpoint on the **callee's** side that accepts an island-authenticated
request — *island A asserts user U is its authenticated member; mint a room token for this
call* — and mints against its own SFU. That is a federation trust boundary: **cage-match by
law**, and the first place a forged vouch would buy a seat in someone else's room.

**Cost, stated rather than buried:** the caller's island becomes a required participant in
cross-island call setup. If it is down the caller cannot place an outbound cross-island call
— acceptable, because a caller whose own island is down has no session and cannot do anything
else either.

### Decision 9b — the foreign-operator exposure, and the answer that is already filed

Surfaced by the app tab from a question of Nick's. **Rewritten after a bad first draft** —
the error is kept below rather than quietly replaced, because it is the more useful half.

**Measured, not assumed** (app tab, `livekit_call_service.dart:152`): `Room.connect` passes
no `e2eeOptions`, and a grep for `e2ee|encrypt|frameCryptor|keyProvider` across
`lib/features/call/` returns nothing. LiveKit decrypts at the SFU by default.

#### The exposure, split into two halves that differ in KIND

Collapsing them lets this read as "same known residual, new recipient", which understates it
exactly where the decision is being made.

| | what the callee's operator gets | covered by an existing accepted residual? | does media E2EE fix it? |
|---|---|---|---|
| **IP + call metadata** | caller's IP (direct client→SFU), timing, duration; under 9a an island-A vouch naming the caller | **Yes** — recorded as *"an operator promise about logging, not a property of the system"* | **No** |
| **Audio + video content** | the media **in the clear** | **No** — same-island hosting meant media only ever reached the operator the user chose | **Yes** |

For a person who is not their member, has no account with them, and never chose them.

- **The metadata half** is an accepted residual **transferred outside the trust
  relationship.** It was accepted about the user's *own* operator, a party they chose; a
  logging promise from a stranger is worth less than one from your own operator, and E2EE
  does not touch it.
- **The content half is the bigger one, and it was never covered by anything.**

#### What the first draft got wrong

1. **"The confidentiality axis genuinely has no prior" — FALSE.** The prior is
   **claude-tasks#3426**, OPEN, labelled `project:aiko-chat-island`, filed by Nick on
   2026-08-25 out of the *same* friends crucible whose residual this section quotes — five
   rows below the line cited, in the same table: *"Media E2EE is orthogonal | Not part of
   this build | Filed as #3426, must not be smuggled in."* The table was read to the row that
   answered the question and no further. Same failure as missing #3170's second comment, in
   the same session.
2. **"E2EE is a design, not a flag" — also false**, and it conflated LiveKit **room-level
   media** E2EE with the **group-message** crucible's pairwise-fanout key-management problem.
   #3426 exists precisely to say those are different doors.

#### What #3426 actually says

LiveKit — already self-hosted on both boxes — has **built-in room-level E2EE via insertable
streams**: applied automatically to all media tracks from all participants plus data channels,
with the SFU forwarding packets it cannot decrypt, and group calls fully supported. So **calls
could be end-to-end encrypted without touching the message path**, the report queue, or the
moderator election. `island_mode` is one flag standing for two orthogonal dials, and the `e2ee`
bolt is holding the *media* door shut for a reason that only applies to the *message* door.

#### So the finding is not "we have no answer" — it is a RE-PRICING

The answer is filed, and its deferral was reasoned. What Decision 9 changes is the **benefit**
side of a question already in front of Nick — #3426's own decision 2, *"Is media E2EE wanted
before Phase B MLS?"*, was priced when the only operator seeing your media was the one you
chose. Under cross-island calling it becomes the thing that keeps an **unchosen foreign
operator** out of your call content.

**The costs in #3426 are unchanged and must not be understated:** server-side recording/egress,
transcription and simulcast layer switching become limited; and splitting `island_mode` into two
signed manifest fields is a **schema + wire change to a signed artifact** — enabling media E2EE
while the manifest still advertises `moderator` is mislabel by omission. The friends crucible's
instruction stands: **it must not be smuggled into another feature, and design 12 does not.**

#### The collision Nick should see as part of the same decision

#3426 names it: under media E2EE **an agent that participates in a call must be a KEYHOLDER,
not an eavesdropper — a pipeline that cannot decrypt cannot run inference.** Resident agents
are shipped (#3096) and the `webrtc://` DataScheme thread has aiko pipelines *consuming* media.
#3426 reads keyholder-not-eavesdropper as *"arguably the right answer"*, but it is a real cost
this product carries that a plain chat product would not — and it should be visible before the
decision, not after.

#### Decision 9c — THE SHIPPING GATE (the thing neither tab put in)

**Cross-island calling MUST NOT ship until #3426 is decided.** Either media E2EE is on, or
the 9d disclosure ships with it and the user consents per call. Shipping with **neither** is
the state this gate exists to prevent.

Recorded as a **precondition**, not a decision, because 9b carried the exposure as *a
decision for Nick* — and **an open decision does not stop a build.** A cold reader on the
app tab's consolidation put it as *"a beautiful, well-logged front door on a house with no
walls."* That overstates today: nothing is built, cross-island calling does not work at all
(Decision 9), same-island calls route through the operator the user chose, and the exposure
was disclosed and re-priced rather than deferred silently. The app tab rejected it on those
grounds and was right to.

**But the structure holds.** If cross-island calling ever ships before #3426 is decided,
that sentence becomes exactly accurate, and nothing in this document would have stopped it.
So the gate is written down as a gate.

E2EE and disclosure **compose rather than duplicate**: E2EE removes the exposure,
disclosure makes it consented. Either satisfies the gate; neither is redundant.

#### Decision 9d — RULING: the user is told, at call time

> **"The user should always know if their call is going to go through an island
> unencrypted."** — Nick, 2026-08-31

*(Given to the app tab and relayed here; recorded with that provenance so a later reader
knows which tab heard it first.)*

Disclosure **at call time, not in a document**. It applies to both parties and bites
hardest under Decision 9, where the hosting operator is one the **caller never chose**.

**This gives #3426's manifest split a job it did not have.** The split has so far been
argued only as *"a manifest must not lie"* — a correctness point. Under this ruling it
becomes **user-facing machinery**: the app reads the signed self-manifest of whichever
island will host and tells the user, before connect, whether media is end-to-end encrypted.
The split stops being a tidiness fix and becomes a **precondition for the disclosure** — a
stronger justification than the one on the ticket, now recorded there.

**Division of labour.** App side: read the hosting island's manifest at video-token time and
surface it before connect *and* in the ring UI for an incoming cross-island call (the callee
is deciding whether to answer), **failing closed** — an absent or unreadable
media-confidentiality field reads as NOT encrypted, never as encrypted. Island side:
**make the field exist, signed, and readable.** That is the whole island half, and it is the
Q1 split already ruled on.

#### Decision 9e — there is no "mostly peer-to-peer anyway" softener

Verified in the client, in code rather than prose (`livekit_call_service.dart:25`, the const
passed at `:156`):

> *"Peer-IP privacy is a HARD requirement (Nick, 2026-08-11): media must never traverse a
> direct path that exposes participant IPs, so all media is forced through TURN. NOT
> flippable and NOT per-island adaptive — `.all` is an explicitly REJECTED fallback."*

`kCallIceTransportPolicy = RTCIceTransportPolicy.relay`. Every call takes **two server hops
by design** — forced TURN relay, then the SFU — and `Room.connect` passes no `e2eeOptions`.

**The mechanism guaranteeing the operator sees everything is the one added to protect
privacy.** Force-relay traded peer-IP visibility for operator visibility, which was the right
trade while the operator was yours. Decision 9 changes who that operator is, and because the
direct path was removed deliberately there is no partial-P2P mitigation to fall back on. That
is why insertable-streams E2EE is not *a* route to content confidentiality here — **it is the
only one.**

#### Decision 9f — force-relay is also a BILLING decision, and its stated rationale does not survive the topology

> **RULING (Nick, 2026-08-31):** *"an app user should be able to decide if they care if they
> expose their IP bc we don't have infinite egress so will have to charge or cap minutes if
> we're passing packets."*

*(Given to the app tab, relayed; provenance recorded.)*

**This amends his own 2026-08-11 ruling**, which 9e quotes and which the client enforces as
`const RTCIceTransportPolicy.relay`, documented *"NOT flippable and NOT per-island
adaptive"*. His to amend — flagged here so the docstring is updated **deliberately** rather
than left citing him as the authority for the opposite of his current position.

**The economic fact nobody priced.** Force-relay means every byte of every call traverses
the island's TURN server, so **egress cost is linear in call-minutes with no direct path to
amortise it.** A decision framed purely as privacy was also a billing decision. Nick's
consequence: charge, or cap minutes. **Nothing on either box meters call egress today** —
that is island-side work and a precondition for any charge-or-cap.

##### And the rationale is calibrated for the wrong topology — VERIFIED

The docstring's reason is that *"`.all` leaks peer IPs"*. The app tab hypothesised that this
is a **mesh/P2P rationale applied to an SFU**, and flagged it explicitly as unverified. It
checks out against LiveKit's primary documentation (`docs.livekit.io/reference/internals/
client-protocol/`):

> a client establishes *"up to two separate `PeerConnection` objects. One for publishing
> tracks to the server, and the other for receiving subscribed tracks"*, and *"client and
> server will exchange ICE candidates via `trickle`"*.

**ICE candidates are exchanged client↔server only. No participant-to-participant path
exists**, and P2P support is an open LiveKit *feature request*, not a mode we could be
falling into. So:

- under `.all`, the party that learns your address is the **operator's SFU**;
- under `.relay`, it is the **operator's TURN server** — the same operator, and on both live
  islands the same physical box (`turn.<domain>` and `livekit.<domain>` resolve together);
- **under neither does another participant learn it**, because there is no channel by which
  they could.

**So force-relay buys approximately nothing on peer-IP privacy in this topology, while
costing 100% of media egress.** Nick's economic instinct is right for a reason he did not
have to hand.

**Scope of that claim, stated honestly:** verified from the documented topology, **not from
a packet capture**. It is strong enough to reopen the decision and *not* strong enough to
flip a hard privacy ruling on its own — a wrong reading re-opens exactly the leak the
2026-08-11 ruling closed. Confirm with a capture, or take it as Nick's call, before changing
the policy.

##### Separate the axes before designing the control

- **Privacy** — who sees my IP: the **operator** (unavoidable in an SFU, under either
  policy) versus the **other participant** (already zero, under either policy).
- **Cost** — who pays for the bytes: relay is 100% egress; direct-to-SFU still hits the
  server; only true P2P is cheap, and an SFU does not offer it.

These are **not the same toggle**. A single "protect my IP" switch that silently also means
"cost the operator egress" misrepresents the trade. Nick's 9d disclosure ruling points at
**one preference surface serving both exposures**, rather than two controls.

#### Unchanged from the first draft, and still the fair framing

- **Not a regression.** Cross-island calling does not work today (Decision 9), so there is no
  working "before". Same-island is unchanged: your own operator already hosts the SFU and reads
  message bodies in cleartext (`should_wake`, `messages.body`). **Own-operator visibility was
  never the protected property** — `_payload`'s opacity is aimed at Apple.
- **Not caused by 9a or by which side was picked.** Whichever island hosts, *some* operator
  sees a non-member's media; mirroring it moves the exposure to the callee and breaks the
  ring-path argument. This is a property of cross-island calling **existing at all**.

Disclosed here per draft ADR-0008's own argument — *"an unnamed centre gets discovered by an
operator rather than disclosed to them"* — which holds equally for an exposure discovered by a
**user**. Gates *shipping* cross-island calling, not building it.

**Scope — and this part is NOT settled.** "The callee" is well defined for a **1:1 ring**,
which is what is shipped and what CallKit is about. But under "calls are gatherings, not
channel properties" a call has its own participant set, and **a gathering across three or
more islands has no single callee.** Host-selection for the gathering case is open, and this
ruling must not be generalised to it. Not urgent — the gathering design is separately gated
on #3196 (Decision 1b) — but it is the first question that design has to answer.

---

## What gets better

`_EXPIRATION_SECONDS = 60` (island) and `kCallInviteFreshness = 10s` (app) are two answers
to one question — *how late is too late for a ring?* — 6× apart, in different repos,
neither aware of the other. The gap between them is where the current bug lives. Native
call UI forces the reconciliation, because a VoIP push should expire exactly when the ring
stops; the two collapse into one number with a physical meaning (ring duration), derived
from call semantics rather than wire latency.

## Sequencing

1. **Carry the call id** (Decision 1) — push payload + v2 sentinel read path. Small.
2. **Wake on the end sentinel** (Decision 5) — the actual island blocker.
3. **`token_kind` + partial-debt safety + capability-aware `/health`** (Decisions 2, 2a).
4. **VoIP topic + preflight, in ONE change with its tests** (Decision 3).
5. **Send-path fork** (Decision 4).
6. **FCM transport** (Decision 8) — independent once 1-2 exist; start the Play declaration
   in parallel, since it is slower than the code.

Decisions 4, 5 and 7 are **one cage-match, not three** — the same trust boundary from three
sides; reviewing them apart is how a fix for one re-opens another. Decision 2a belongs in
that review too: it is where a schema change becomes a stranger's phone ringing.

Gated separately: **Decision 9 (#3196)** before shipping to cross-island pairs, and
**Decision 1b** (the gathering ACL) which must not start before #3196 settles.

## Rulings and open questions

**Settled (Nick, 2026-08-30):**

- **Placeholder string = `Aiko`** (Decision 6).
- **`includesCallsInRecents`: appearing in Recents is fine** (Decision 6a). Set explicitly
  to `true`, because Apple does not publish the default.

- **Cross-island calls are hosted on the CALLEE's island** (Decision 9). Settles
  host-selection for 1:1; **the gathering case is explicitly not settled.**

- **Play `USE_FULL_SCREEN_INTENT` declaration: Nick submits, justification drafted**
  (Decision 8) — claude-tasks#3615, slugged to `aiko_chat_app`. The runtime-prompt +
  graceful-degradation path is required **regardless of the outcome**, so it is build work
  either way, not a branch on the review.

**Settled (Nick, 2026-08-31):**

- **The user is told at call time whether media is unencrypted** (Decision 9d) — which makes
  the Q1 split below a precondition for that disclosure, not a tidiness fix.
- **`island_mode` SPLITS into two signed manifest fields** (#3426 Q1) — decided *ahead* of
  whether media E2EE is ever enabled, because one flag governing two orthogonal properties is
  a manifest that eventually lies. Additive migration + two signed fields; wire half agreed
  with the app tab before either merges, island deploys first. It also converts Q2 from a
  schema change into a policy call against a field that will already exist.
- **Gathering host-selection across 3+ islands — CLOSED as a live question.** Never a
  decision anyone was waiting on: it is the first question the *gathering-with-ACL* design
  must answer, and that design is gated on #3196. It stays recorded in Decision 9's scope
  note; carrying it on an open list only inflated the list.

**Still open:**

- **Media E2EE on/off** (#3426 Q2, Decision 9b) — a policy call, not a build. **Q3 is
  ANSWERED** (2026-08-31): nothing relies on server-side media access — the island's whole
  LiveKit surface is minting JWTs, and there is no egress config or container on either box,
  no recording, no transcription. The one real cost is **simulcast**, which the client
  deliberately enables; LiveKit's own E2EE docs do not state that limitation, so it is
  unverified in both directions. Carries the **agent-as-keyholder** collision, which wants
  its own design and must not ride along.
- **Call-egress metering** (Decision 9f) — nothing on either box meters it, and it is the
  precondition for the charge-or-cap Nick names. Island-side.
- **Whether force-relay stays** (Decision 9f) — its stated rationale does not survive the SFU
  topology, and Nick has already moved to making IP exposure a user choice. Needs a packet
  capture or his explicit call; **do not flip it on the doc reading alone.**
- **GATE (Decision 9c): cross-island calling does not ship until #3426 is decided** — media
  E2EE on, or the 9d disclosure shipping with it. Neither is what the gate prevents.
- **The Play `USE_FULL_SCREEN_INTENT` declaration** — Nick submits (#3615). Listed here only
  because it is unsubmitted; nothing waits on the outcome, since prompt-and-degrade is build
  work either way.
- **The media plane has no authoritative config source** (#3685, found 2026-08-31) — neither
  box is a git checkout, and the two `livekit.yaml` files have structurally diverged (40 lines
  vs 23; `redis` and `webhook` sections present on one island, absent on the other). Not a
  blocker on this design, but it is the substrate every call in it runs on — and Decision 9
  now puts a foreign island's users onto it.

## Provenance

Claude (island tab), 2026-08-29, revised the same evening after the app tab's answers.
Grounded in `push_service.py`, `apns.py`, `models.py`, `config.py`, tracker #3170 read
end-to-end (both comments — the second is where Decision 1b comes from), and Apple's
documentation JSON for the two CallKit symbols quoted. Claims about client internals are
the app tab's, attributed as such. The `reportCall(with:updated:)` and
`includesCallsInRecents` declarations are verified; `includesCallsInRecents`'s **default is
NOT verified because Apple does not publish it**, and no behaviour beyond the quoted
abstract should be inferred from this note.
