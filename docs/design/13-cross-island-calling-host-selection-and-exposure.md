# Design 13 — Cross-island calling: host selection, trust, and what it exposes

**Split out of design 12 on 2026-08-31.** Everything below was written there, where it
accumulated because CallKit made these questions urgent. Urgency is not subject: this is
the **#3196 cross-island calling design**, and it would be equally true if native call UI
were cancelled tomorrow. Design 12 keeps §1-§8 (a build doc for one feature) plus a
pointer and the shipping gate restated inline.

**Decision numbers are deliberately NOT renumbered.** `Decision 9`, `9a`-`9f` are cited
from `nickmeinhold/claude-tasks` #3426, #3717 and #3718, from design 12, and from the
`aiko_chat_app` tab's record. A tidier numbering would break every one of those citations
to buy nothing. The labels are identifiers, not an outline.

**How to read the seam.** Design 12 asks *what does native call UI cost the island*.
This asks *when a call crosses an island boundary, whose infrastructure carries it and
what does that expose*. The first is a client-feature question; the second is a
federation-trust question. They met because CallKit turns a quiet cross-island failure
into a full-screen ring for a call the callee cannot join — but that is the only place
they touch.

---

## Decision 9 — #3196 is a hard gate, and CallKit makes it louder

Cross-island calling is **broken today**: `room_for_channel()` is gateway_id-namespaced
(`imagineering:` vs `enspyr:`) and the two islands run separate SFUs. That was found during
the temper and filed as #3196, and it is the reason the gathering design is held.

Under CallKit the failure gets much louder: **a full-screen ring, through DND, for a call
the callee physically cannot join.** Today the same failure is a quiet dead end.

### DECIDED (Nick, 2026-08-30): the CALLEE's island hosts the call

This settles host-selection, which was the fork #3196 needed.

**Attribution, because it matters downstream:** the DECISION is Nick's. The argument below is
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

#### Decision 9d — DECIDED: the user is told, at call time

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

> **DECIDED (Nick, 2026-08-31):** *"an app user should be able to decide if they care if they
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

##### Strengthened 2026-08-31 — from vendor prose to structure and to the live boxes

The claim above rested on LiveKit's documentation. Three checks since have moved it onto
harder ground:

1. **Structural, not prose.** `livekit/protocol` `livekit_rtc.proto` defines
   `TrickleRequest { candidateInit, SignalTarget target }`, and `SignalTarget` has exactly
   two values — `PUBLISHER` and `SUBSCRIBER`. There is **no participant-scoped candidate
   message anywhere in the signalling proto.** Two participants cannot exchange ICE
   candidates on this stack even in principle.
2. **The other leak channel is closed too.** `livekit_models.proto`: `ParticipantInfo` —
   the struct the server broadcasts *about* you to everyone else — has no address field.
   The only `address` in the file is on `ClientInfo`, which travels client→server in the
   join request and is never forwarded on.
3. **Same process, not merely the same box** (verified on both islands, 2026-08-31). The
   TURN is LiveKit's **embedded** TURN: the `turn:` block lives inside each box's
   `livekit.yaml`, and `docker ps` shows one `livekit/livekit-server:v1.13.5` container per
   box with **no coturn anywhere**. So force-relay does not route you past the operator's
   SFU — it routes you *through the same process*.

4. **Neither remaining leak channel is open** (checked 2026-08-31 rather than delegated).
   In the pinned SDK (`livekit_client-2.8.1`) every candidate path is scoped to
   `SignalTarget.PUBLISHER` or `SUBSCRIBER` — the two local `PeerConnection`s — and inbound
   `trickle` is routed to one of those two and nowhere else (`signal_client.dart:276`,
   `engine.dart:634-648`). There is no participant dimension, and nothing is exposed to app
   code. Server-side, the **only** use of `ClientInfo.Address` in `livekit/livekit` is
   `GetRegionSettings(p.params.ClientInfo.Address)` in `participant_signal.go:281` — geo
   region selection returned to *that same participant*, never broadcast.

The supporting mechanism is worth stating because it also closes the loop with 9b: an SFU
**terminates and re-originates** rather than forwarding. Separate ICE, DTLS and SRTP per
`PeerConnection`; the callee's socket only ever receives packets sourced from the SFU.
**The existence of insertable-streams E2EE is itself the proof of termination** — there
would be nothing to encrypt against, and no simulcast question, if the server were merely
passing packets through.

**Scope of that claim, stated honestly:** still **not a packet capture**, and this does not
license flipping the policy on its own. But the *reason* to keep `.relay` has changed. It
buys approximately nothing on peer-IP privacy; it does buy **NAT traversal from restrictive
networks** and, on our boxes, **forcing media over 443 through the SNI mux** so it reads as
HTTPS to a middlebox. Those are reachability and traffic-shape properties, they are real,
and they are not what the docstring claims.

That inversion is the actual defect, and it is more dangerous than a wrong comment: the next
reader to falsify *"it protects peer IPs"* — as this section just did — has a clean-looking
argument for deleting something we need. **Keep the constant; fix the reason.** What the
capture on claude-tasks#3717 is still for is narrower than when it was filed — whether `.all`
would ever beat `.relay` on connect success or setup latency. The privacy half no longer
needs sniffing.

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
decision must not be generalised to it. Not urgent — the gathering design is separately gated
on #3196 (Decision 1b) — but it is the first question that design has to answer.

---

## Decisions and open questions

*(Split from design 12 with the decisions above. §1-§8 items stayed there.)*

**Settled:**

- **Cross-island calls are hosted on the CALLEE's island** (Decision 9). Settles
  host-selection for 1:1; **the gathering case is explicitly not settled.**
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

**Still open:**

- **Media E2EE on/off** (#3426 Q2, Decision 9b) — a policy call, not a build. **Q3 is
  ANSWERED** (2026-08-31): nothing relies on server-side media access — the island's whole
  LiveKit surface is minting JWTs, and there is no egress config or container on either box,
  no recording, no transcription. **The simulcast cost is now ANSWERED too** (2026-08-31,
  claude-tasks#3715): it is not real for VP8, which is what the client publishes. LiveKit's
  frame cryptor leaves the VP8 payload descriptor — temporal-layer id and keyframe bits —
  outside the ciphertext *by design* (`FrameCryptor.ts`: *"This is fine as the SFU keeps
  having access to it for routing"*), the native cryptor the Flutter client actually runs
  carves out the same 10/3 header bytes, and `encryption path:pkg/sfu` returns **zero
  results** across the server's entire forwarding subsystem. The real E2EE costs are **AV1**
  (refused outright) and H.264/H.265 (NALU handling) — neither is our codec, which makes the
  client's `videoCodec: 'vp8'` pin load-bearing if E2EE is enabled. The app tab reached the same conclusion by a
  different route — reading the shipped SDK rather than the cryptors — and found the only
  two things `e2eeOptions` changes at all: `backupVideoCodec: enabled: false` and
  `disableRed: true`. Simulcast is never touched. The backup codec is already inert for us
  *because* of the `vp8` pin (it exists to fall back from VP9/AV1 **to** VP8, and we are
  already at the floor). **Audio RED is also a non-cost here, checked against the version
  the app actually resolves** (`livekit_client 2.10.0`, per `pubspec.lock` — not the 2.11.0
  sitting beside it in the pub cache): `AudioPublishOptions.red` defaults to `true` and
  `local.dart:189` reads `disableRed: room.e2eeManager != null ? true : publishOptions.red
  ?? true`, so RED is already disabled on our path with or without E2EE. (That inversion —
  `red: true` producing `disableRed: true` — looks like an SDK bug, but it makes the cost
  zero either way.) **So Q2's cost column is empty and its benefit side has risen; it is now
  a pure policy call.** Scope: a source read of both cryptors, the server, and the resolved
  client SDK — not a live A/B capture of layer switching. Carries the **agent-as-keyholder** collision, which wants
  its own design and must not ride along.
- **Call-egress metering** (Decision 9f) — nothing on either box meters it, and it is the
  precondition for the charge-or-cap Nick names. Island-side.
- **Whether force-relay stays** (Decision 9f) — its stated rationale does not survive the SFU
  topology, and Nick has already moved to making IP exposure a user choice. Needs a packet
  capture or his explicit call; **do not flip it on the doc reading alone.**
- **GATE (Decision 9c): cross-island calling does not ship until #3426 is decided** — media
  E2EE on, or the 9d disclosure shipping with it. Neither is what the gate prevents.
- **The media plane has no authoritative config source** (#3685, found 2026-08-31) — neither
  box is a git checkout, and the two `livekit.yaml` files have structurally diverged (40 lines
  vs 23; `redis` and `webhook` sections present on one island, absent on the other). Not a
  blocker on this design, but it is the substrate every call in it runs on — and Decision 9
  now puts a foreign island's users onto it.

---

## Provenance

Split from `12-native-call-ui-callkit-connectionservice.md` on 2026-08-31; see that
document for the CallKit design these decisions were recorded alongside, and for the
shipping gate (Decision 9c) restated there so a builder meets it without following a
pointer. Authorship, sourcing and scope stamps are unchanged from the original text —
including the ones that mark a claim as NOT verified.
