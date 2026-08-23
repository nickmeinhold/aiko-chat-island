# ADR-0008: Push topology — who is allowed to ring a handset

> **DRAFT, NOT FILED.** Destined for `aiko_chat/docs/adr/` via PR, per Nick's
> 2026-08-23 homing ruling (app+island decisions live in `aiko_chat`). Held here
> in the island repo, beside the research it distils, until Nick has read it and
> decided when to open the PR. **Andy has not seen this**; he was explicitly told
> not to spend time on the topic today.
>
> Numbering continues the existing project-level series (0001-0007) rather than
> starting a fresh one, so that existing bare-number citations keep resolving.

| | |
|---|---|
| **ADR** | 0008 |
| **Status** | Draft, requesting comments |
| **Owner** | Nick Meinhold |
| **Created** | 2026-08-23 |
| **Thread** | *(Discussion link once posted)* |
| **Reference** | `aiko-chat-island/docs/crucible/push-federation/RESEARCH.md` (full option space, prior art, open premises) |

## Summary

Three claims. **One:** an APNs credential cannot be scoped to a subset of users,
so "every island operator holds the key" means every operator can ring every user
of the app — and the binding problem is *revocation*, not secrecy. **Two:** this
is a platform-specific constraint, not a property of push: Web Push binds its
credential to the *server*, so a federation of independent ringers is the
standard's native shape, and the target is therefore **Web Push as the island's
egress contract**, with a relay adapting to APNs for Apple. **Three:** the
irreducible singleton is a legal one — Apple issues to an entity — and the answer
is that the entity should be a **foundation**, not a person.

**Decision today: change nothing.** Islands keep talking to APNs directly. The
trigger for building the above is the first island operator who is not Nick.

## Motivation

**The app tab named this hole two months before the credential existed.** App
Design 06 (sovereign identity federation, captured 2026-06-29) points forward to
Design 07 as "the one place this model meets a platform wall (APNs/FCM) that
subtraction can't remove", and Design 07 (2026-06-30) states the thesis directly:

> Everywhere else, federation gets simpler by moving cleverness to identity (06).
> Notifications are the exception: APNs and FCM bind push delivery to one app
> identity — one bundle id, one signing key, owned by the app vendor. […] the
> design move here is not "decentralise it" — it's **quarantine the
> centralisation**.

So this is not a question raised by a deploy; it is a known, named gap in the
federation model being answered at the moment it stopped being hypothetical. What
made it urgent rather than merely open is that the island gained an APNs send path
(island #3267) and a credential went onto a production box on 2026-08-23 — the
first time the wall had anything real behind it. **When there are other operators,
how do they ring?**

**This ADR converges with Design 07 rather than originating.** Both were derived
independently — Design 07 from the app side in June, this from the island side in
August, off the `push-federation/RESEARCH.md` option space — and both land on the
same architecture: a small relay quarantining the platform credential, everything
else federated, explicitly copying Mastodon. Two independent derivations reaching
the same shape is the strongest evidence either has, and it belongs in the record
rather than being quietly re-presented as new. Where they differ is layering, and
that difference is live, not settled: Design 07 models the registration wire on
**Matrix's `pushers/set`**, while this ADR makes **Web Push (RFC 8030) the
island's egress contract**. Those are compatible in principle — Design 07 already
lists a UnifiedPush endpoint as a valid `pushkey` — but nobody has reconciled them
line by line, and doing so is a precondition of filing, not a follow-up.

Answering it badly is expensive in a specific way. Apple's `.p8` is issued to a
Developer Team; you name the `apns-topic` per request; that is the entire access
model. There is no "this credential may ring users of island X but not island Y",
and no reason to expect one. So handing the key to operators is not a *degree* of
trust — it is: any operator can ring any user of the app, with any alert text, at
any hour.

**The discriminator is revocation.** With N key-holders, ejecting one means
rotating the key, which invalidates it for everyone; every island must redeploy in
a coordinated window, and until they do, nobody's phone rings. You cannot remove
one bad operator without an outage for every good one — and you only ever rotate
under time pressure, because you only rotate when something has already gone
wrong. With a relay, revoking an operator is deleting a row. The asymmetry is
permanent and worsens with each operator added.

## Proposal

**Guide-level.** An island never speaks to Apple. It speaks *Web Push* — the same
protocol it would use to reach a browser or an Android handset — to a per-device
endpoint URL the device gave it. For Apple devices that endpoint belongs to a
relay, which translates Web Push into APNs. The relay holds the platform key and
nothing else: it cannot read notification contents, because Web Push payloads are
encrypted between island and device. The developer account that the key belongs to
is held by a foundation, not an individual.

**Reference-level.**

1. **Credential binding decides everything.** Three push systems, three different
   things the credential is bound to:

   | system | bound to | one app, many independent ringers? |
   |---|---|---|
   | APNs (iOS, macOS) | the **app** (bundle id) | **impossible** |
   | Web Push / VAPID | the **server** | native to the design |
   | UnifiedPush (Android) | the **user's chosen distributor** | native to the design |

   A single client app is therefore fine. The contradiction bites only on Apple
   platforms — and macOS inherits it, it is not "just an iPhone thing".

2. **Web Push is the island's egress contract** (RFC 8030; payload encryption
   RFC 8291). One protocol for every platform. Islands hold no APNs code.

3. **The Apple relay is a protocol adapter.** Web Push in, APNs out, translating
   TTL / Urgency / Topic into expiration, priority and collapse-id. It is
   content-blind *cryptographically*, not by policy.

4. **The per-device endpoint URL is the capability.** It is an opaque bearer
   token the device hands to its island; possession is permission to ring that
   device, and nothing else. No bespoke ticket format is required.

5. **Peer-provided ringing is the right abstraction on every platform.** The
   island-facing contract is "ask something that can reach this device to ring
   it". On Web and Android the set of things that can reach it is open; on Apple
   it degenerates to one member. **A degenerate case, not a special case** — and
   the degeneracy is Apple's, visible in the data, and evaporates the moment a
   platform allows it.

6. **A foundation holds the developer account.** Apple's organisation enrollment
   requires a legal entity with a D-U-N-S number; non-profits qualify and hold
   developer accounts routinely. The singleton does not vanish — it becomes a
   **governed commons instead of a person**. This buys continuity beyond any
   individual's Apple ID, and it makes "who may run a relay" a written process
   rather than goodwill.

7. **Nothing is built now.** One operator, two islands, and the app publisher is
   the same person, so a relay today is a new deployable on the critical path of
   every notification bought with zero current benefit. **Trigger: the first
   island operator who is not Nick.**

**In force immediately, because they cost nothing and preserve every option:**

- **Count key-holders in OPERATORS, not in boxes.** The `.p8` is currently on both
  islands (imagineering and enspyr, 2026-08-23) and that is fine and consistent
  with the decision above: "islands keep talking to APNs directly" *requires* the
  credential on each island that rings. The revocation argument in the Motivation
  is about **N operators**, not N machines — both boxes are Nick's, so N is still
  1, and there is nobody to eject. What must not happen without revisiting this
  ADR is the credential reaching a box **Nick does not control**, because that is
  the step that makes rotation a multi-party outage instead of a redeploy.
  *(An earlier draft said "do not copy the `.p8` to a second island" here, which
  directly contradicted the decision two sections up and was reasonably read as
  licensing the opposite. The commitment was wrong, not the deployment.)*
- **Keep the island's APNs seam narrow.** It is currently four functions with one
  caller of `send()`; swapping direct-APNs for a Web Push client is one module.
- **Do not regress the 410-only reaping rule.** Reaping a device on `400
  BadDeviceToken` would delete the entire device table the first time an
  environment flag was set wrongly, because Apple returns `BadDeviceToken` both
  for a dead token and for a live token sent to the wrong environment.

## Rationale and alternatives

- **Why not one relay we simply run, and stop theorising?** That is the current
  de-facto answer and this ADR does not overturn it for today. What it refuses is
  leaving it *unnamed*: an unnamed centre gets discovered by an operator rather
  than disclosed to them.
- **Why not give each operator the key and trust them?** Revocation, above. This
  is not a judgement about operators; it is that the platform gives us no way to
  un-trust one.
- **Why not have each operator publish their own app?** This is a real option and
  the fediverse runs it (see Prior art) — Apple's bundle-id welding then becomes
  the isolation boundary *for free*, and a bad publisher's blast radius is exactly
  the users who chose it. Rejected as the *primary* answer only because it forces
  a client-per-operator, which is a worse product for the common case. It remains
  available and composes with this proposal.
- **Why not attested enclaves holding the key, so several parties operate ringing
  without holding it?** Genuinely sound, and the direct analogue of the DNS root
  KSK living in HSMs under ceremony. Rejected *for now* as premature: Mastodon
  needs no TEE because encryption makes the relay content-blind and the bundle id
  partitions publishers. Reach for the standard before the enclave.

## Prior art

**Closest to home: app Design 07, "notifications — federation-ready" (2026-06-30),
`aiko_chat_app/docs/design/07-notifications-federation-ready.html`.** It reaches
this ADR's architecture from the app side, two months earlier and independently:
quarantine the centralisation in one relay; keep accounts, channels, history,
identity and notify logic (rules, mute, mentions) on each home gateway; copy
Mastodon rather than invent. It also already sketches the registration wire on
Matrix's `pushers/set`. Read it before filing this — see the Motivation note on
reconciling its layering with Web-Push-as-egress.

**Mastodon runs almost exactly this, in production, at fediverse scale.**

- `mastodon/webpush-apn-relay` — an official Mastodon repo; a relay forwarding
  Web Push to APNs. <https://github.com/mastodon/webpush-apn-relay>
- `DagAgren/toot-relay` — a *third-party* relay, "built for Toot!.app but usable
  for anyone" — i.e. the many-relays-per-publisher model, also live.
  <https://github.com/DagAgren/toot-relay>
- Mastodon's push API. <https://docs.joinmastodon.org/methods/push/>

Instances issue standard RFC 8030 POSTs to a per-device endpoint and never learn
APNs exists. The relay can be built so it has no access to notification contents.

**Mastodon gGmbH — a non-profit — holds the developer account and runs the
official relay for a federated network.** That is this exact problem, already
solved. Mozilla, the Signal Technology Foundation, Wikimedia, Blender and The
Document Foundation likewise hold developer accounts.

**DNS is the analogy to reason with, but read it correctly.** The root *servers*
replicate **public** data — replication is free because there is no secret. The
root **KSK is not on them**; it lives with IANA in two facilities, in HSMs, under
M-of-N ceremony. The lesson is *distribute the serving, concentrate the signing*
— not "spread the key across the servers". And DNSSEC survives that concentration
only because signatures are **verifiable**; an APNs JWT is a bearer credential
where possession is authority and there is nowhere for a bad ring to be caught.

## Unresolved questions

1. **Andy's registrar-of-registrars.** If relay/ringer discovery is a delegation
   table, it plausibly belongs in the same structure. Nobody has asked him what it
   actually is; the argument here rests on a one-sentence description. **Highest-
   yield open item.**
2. **Can an APNs auth key be restricted to a single bundle id?** Believed yes.
   Narrows blast radius; does not change the conclusion.
3. **Who constitutes the foundation, in what jurisdiction, governed how?** A
   badly-governed foundation is worse than a trusted individual, and a foundation
   with one member is one person with more paperwork.
4. **Apple retains a veto regardless.** A foundation changes who holds the
   relationship, not that Apple can end it. The honest claim is *"the ringing is
   federated; the app's identity is not."*
5. **Does this make `aiko_chat` the home of the project-level ADR series?** Filing
   0008 here implies it. Worth stating deliberately rather than establishing by
   accident.
6. **Reconcile the registration wire with app Design 07. BLOCKS FILING.** Design 07
   models it on Matrix's `pushers/set`; this ADR makes Web Push (RFC 8030) the
   island's egress contract. Believed compatible — Design 07 already admits a
   UnifiedPush endpoint as a `pushkey` — but "believed compatible" is exactly the
   shape of claim that has cost this project rounds before. An ADR that becomes
   the decision of record while silently diverging from a design doc in the same
   repo family creates the drift it exists to prevent. One read-through of Design
   07 §"Wire shapes" against the Proposal here settles it.

## Rejected ideas

- **Threshold-signing the APNs JWT** (k-of-n operators jointly mint, so no single
  party can forge). Does not work: the JWT is a bearer token valid for an hour
  against every device and topic in the team, and is deliberately cached and
  reused because Apple rejects providers that mint too often. The ceremony would
  authorise *"the group minted a token"*, not *"the group approved this ring"*.
- **A bespoke ring-ticket format.** Designed, then discarded on finding that the
  Web Push endpoint URL already is one, standardised.
- **`event_id_only` as the privacy mechanism.** Superseded by RFC 8291 payload
  encryption: blindness by construction beats blindness by good behaviour. (The
  island's current payload — a fixed generic alert plus a channel id — matches the
  spirit and should be kept until the transport changes.)

## Consequences

Island task #3 (a per-token `apns_environment`, so one island serves both
development and TestFlight builds) remains correct and should still be built: it
is needed for exactly as long as the island talks to APNs directly, which under
this ADR is "until the trigger fires", i.e. indefinitely for now.
