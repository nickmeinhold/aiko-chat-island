# Change: Network-level moderation — defederation, not takedown

Status: Draft for discussion. Not a decided spec, and **not legal advice** — it is
an engineer's summary of two adversarially-verified research passes (US and
Australian law), written so a lawyer can be handed something concrete. Load-bearing
legal claims are cited inline to primary statute or a regulator's own guidance;
every claim below survived a 3-vote adversarial verification pass, and two claims
that failed it are recorded in "What we do **not** claim". Spec deltas and a task
breakdown are deferred until the direction is agreed. Get real legal advice before
relying on the runbook in Part B.

## Why

Everything we have shipped moderates *within* an island: message takedown (#7 /
PR #104, the forward-ULID retraction), per-island user ban, per-island report
queue. **Nothing removes an entire node from the federation.** A bad-faith operator
can stand up an island hosting CSAM and federate it, and by construction there is
no central kill-switch — because sovereignty here *is* key-as-identity
([`concept_federation_fork_key_as_identity`]), and no central authority means no
central revocation.

That is not a gap we forgot to fill. It is the load-bearing wall. The property that
makes the architecture worth building (no chokepoint, no global root — see
[`06-identity-and-trust`] and [`concept_directory_discovery_bootstrap_not_centralization`])
is the same property that makes "take down the bad island" impossible to answer at
the protocol layer. This is *the* unsolved problem of decentralized social. The
fediverse answers it with defederation and instance blocklists; Matrix with
server-ACLs and room bans. We had not answered it at all.

The honest counter-cost, stated up front: **no central kill-switch is the point of
the architecture, and it is also how you get a pedo-island.** We do not get to keep
the first without owning the second. The purpose of this note is to see the shape
of that trade clearly, choose a defensible posture *before* federation goes live,
and write down what an operator actually has to do when illegal content lands — so
the choice is made in daylight, not under a removal notice with a 24-hour clock
running.

## What Changes

This note makes one reframe and proposes four decisions. The reframe is the whole
game; the decisions fall out of it.

**The reframe — "central takedown vs. defederation" is a false fork.** The research
is unambiguous: **central takedown does not exist anywhere in production
federation.** The only genuine admission-control mechanism found across the entire
fediverse is Mastodon's `LIMITED_FEDERATION_MODE` (default-deny, federate only with
manually-approved servers) [Mastodon admin docs]. Everything else — FediCheck,
shared blocklists, Mjolnir ACLs, Matrix server-ACLs — is *after-the-fact denylist
cutoff*. So the real axis is not "weak defederation vs. strong takedown". It is:

> **open-then-cut** (federate with anyone, block bad actors reactively, and eat the
> liability for whatever transited or cached on your box in the meantime)
> **vs. allow-list-then-admit** (default-deny, peer only with vetted islands, so the
> bad content never has a path onto your node in the first place).

Posing it as "weak vs. strong" smuggled in an assumption that a strong protocol-level
takedown lever exists to be chosen. It does not. Recognising that dissolves the fork
the same way "filter vs. don't-filter" dissolved on #7 — a bad option-frame is
itself a finding ([`concept_add_remove_asymmetry_never_hide_a_hide`] is the sibling
lesson: the interesting move was seeing the two options were answers to *different
questions*).

The four decisions:

- **Adopt allow-list federation as the default island posture.** For a solo
  sovereign island, default-deny peering is not the restrictive option — it is the
  only survivable one. Start closed; open to a peer deliberately, per-peer.
- **Separate the two layers cleanly.** The **protocol layer** gets a *mechanism*
  (allow-list peering, and later an optional plural pre-filter). The
  **legal/physical layer** gets a *runbook*, not a feature. Conflating them is the
  category error this whole note exists to avoid.
- **Reuse the retraction machinery as the "make-inaccessible" primitive.** The
  forward-ULID retraction shipped in #7 is already the mechanism that removes a
  message from every client's view monotonically. The CSAM runbook's
  "make inaccessible" step is that mechanism, not new code.
- **Defer, do not build, a protocol-level moderation gate.** Matrix's Policy Servers
  (MSC4284) are the one transferable *mechanism* — plural, opt-in, per-room
  pre-delivery gating — but they are premature for aiko today (federation between
  islands is not even live yet). Name the pattern; build it only if and when
  cross-island peering creates the need.

## Impact

- **Affected specs:** [`02-bus-decouple-and-islands`] (federation model),
  [`06-identity-and-trust`] (the trust roster is what an allow-list is built on),
  and the app-repo sybil-resistance design (Design 09).
- **Affected code (island):** a per-island **peer allow-list** (which remote islands
  this node will federate with) — this is new and does not exist yet; the retraction
  path (#7, reused as "make-inaccessible"); the moderation/report service (adds an
  operator-facing "CSAM incident" path distinct from ordinary takedown, because the
  legal obligations differ). No new schema is proposed here; that is spec-delta work
  after agreement.
- **Affected code (app):** none required for the posture decision. If the plural
  pre-filter is ever built, the app surfaces peer-trust state.
- **Affected operations (the first-class deliverable):** a written operator runbook
  (Part B below) that lives with the deploy runbooks, plus a one-time legal-advice
  engagement to pressure-test the "is a solo node a *provider*?" question that the
  research leaves open and that every downstream obligation hinges on.

---

## Design

### Context — what we have, what is missing

An aiko island is a mesh: gateway + broker + registrar + ChatServer
([`concept_island_is_mesh_not_broker`]). The gateway terminates plaintext — it
persists messages to SQLite, runs the report queue, executes takedowns. That
plaintext visibility is *deliberate*: it is what makes within-island moderation
possible at all. Hold that fact; it returns below as the central legal tension.

Two islands run today (`chat.imagineering.cc`, `chat.enspyr.co`) but they are
**independent, not yet federated with each other**. So the island-takedown problem
is, right now, theoretical — which is exactly why now is the time to choose the
posture. Adopting default-deny peering *before* federation exists is free; adopting
it after means a migration and an exposure window. This is the "cage before monster"
ordering: build the throttle before the traffic.

### The frame: why the false fork matters

Last session's #7 turned on recognising that adds and removes are different *kinds*
of event, which dissolved a filter-vs-don't-filter fork that had eaten a whole
cage-match round. The same heuristic fires here one level up. "Defederation vs.
central takedown" reads as two ends of one dial. The research shows there is no dial:

- **No production protocol prevents a bad instance from *ever* federating**, except
  Mastodon's `LIMITED_FEDERATION_MODE` allow-list, which its own docs call
  "intended for private use only... effectively creates a data silo" [Mastodon admin
  docs]. Every other mechanism is reactive cutoff.
- **Matrix Policy Servers** (MSC4284, stable in spec v1.18, 2026-03) are the closest
  thing to pre-emptive gating: rooms *opt in* to a designated policy server that
  issues an accept/reject opinion on each event *before* delivery; rejected events
  are soft-failed — persisted server-side but never shown to clients
  [matrix.org, 2025-04]. Note the shape: **per-room, opt-in, plural** — a gate that
  stays compatible with no-central-authority. And note it is the structural
  *inverse* of our retraction: a retraction is a "remove" applied *after* delivery;
  a policy-server verdict is a gate applied *before* it. Same monotonic-visibility
  spine, opposite side of the delivery boundary.

So the design space is not "how strong a takedown do we build". It is "open-then-cut
or allow-list-then-admit", plus "what is the out-of-protocol legal runbook". Those
are the two real questions, at two different layers, and the rest of this note keeps
them apart on purpose.

### Goals and non-goals

**Goals**

- Choose a federation-peering posture that a single operator can actually survive.
- Give an operator a concrete, jurisdiction-correct procedure for when illegal
  content (specifically CSAM) lands on their node.
- Keep every mechanism compatible with key-as-identity and no-global-root — no
  decision here may reintroduce a central authority by the back door.

**Non-goals**

- We are **not** building a network-wide takedown or a central kill-switch. It is
  architecturally impossible and this note does not pretend otherwise
  ([`concept_sovereignty_scoped_moderation`]: moderation is per-island by
  construction).
- We are **not** building CSAM hash-scanning in this pass (the tooling reality below
  makes that a much larger, separately-scoped question).
- We are **not** solving proof-of-personhood or sybil resistance here (Design 09).

### Decisions

**D1 — Allow-list federation is the default island posture.**

Default-deny peering. An island federates with another island only after its
operator explicitly adds that peer. This is `LIMITED_FEDERATION_MODE`'s model,
generalised to aiko's mesh, and it is the *only* admission-control pattern that
exists in production anywhere.

Critically, distinguish two layers of "open" that the word "federation" blurs:

- **Peer-openness** (which *other islands* this node federates with) — this is what
  the allow-list closes. Default-deny.
- **User-openness** (whether a *person* can sign up on your island) — untouched. An
  island can still run open user registration; what changes is that islands peer
  selectively.

This preserves most of the open-federation dream: a person joins an island freely;
islands form a *deliberately-woven* mesh of vetted peers rather than an
anyone-can-connect graph. The cost is real and named: **frictionless
introduction-of-strangers between islands is gone.** That is the same counter-cost
[`06-identity-and-trust`] already accepted for going CA-free ("going CA-free costs
scalable introduction-of-strangers, and no surveyed system solved that without a
directory"). D1 pays that cost consciously rather than discovering it under a
removal notice.

**D2 — Two-layer separation: protocol mechanism vs. legal runbook.**

The protocol layer answers "whose content can reach my node" (D1). The
legal/physical layer answers "what must I *do* when illegal content is on my node" —
and that answer is a **runbook, not a feature** (Part B). The crypto layer has no
central authority; the physical layer it runs on is drowning in them — hosting
providers, DNS, jurisdiction, law enforcement. Every "there is no kill-switch"
statement is true *only at the crypto layer*. The levers that actually stop CSAM in
the real world all live at the layer below, where sovereignty was never claimed.

**D3 — Reuse the #7 retraction as the "make-inaccessible" primitive.**

The CSAM runbook requires "make the content inaccessible to users immediately"
[IFTAS]. We already built exactly that: the forward-ULID retraction removes a message
from every client's view, monotonically, riding existing forward paths so no client
can miss it. The runbook's make-inaccessible step *is* a retraction. No new
mechanism. (One delta to spec later: a CSAM retraction must also **purge the stored
plaintext/media**, not merely mark it retracted, because "inaccessible to users" and
"not in my possession" are different legal tests — see Part B.)

**D4 — Name the Policy-Server pattern; do not build it yet.**

If cross-island peering ever needs *content*-level gating (not just peer-level
allow/deny), MSC4284's plural opt-in pre-filter is the pattern that does not betray
sovereignty. It is out of scope now: islands do not federate yet, and a pre-filter
with no peers to filter is machinery ahead of need. Recorded so the wheel is not
reinvented.

### The central legal tension (this reshapes the roadmap)

**The gateway's plaintext visibility — the property that powers our moderation — is
also what puts us inside the proactive-detection obligations and denies us the
encryption carve-out.**

Both regimes we researched exempt *end-to-end-encrypted* content from proactive
scanning, for the obvious reason that you cannot scan what you cannot see:

- Matrix scans **unencrypted content only** (Cloudflare CSAM APIs + IWF hash/URL/
  keyword lists); E2EE rooms are not scanned because the homeserver holds only
  ciphertext [matrix.org, 2025-02].
- Australia's DIS Industry Standard imposes proactive detect-and-remove of known
  CSAM on "end-user managed hosting services" but carries an **explicit carve-out
  that E2EE providers need not break or weaken encryption** [eSafety DIS Standard
  fact sheet, 2024].

aiko deliberately terminates plaintext at the gateway *so that it can moderate*
(takedown, retraction, report queue all require the gateway to see message bodies).
That is a defensible choice — but it has a consequence that must be stated:
**moderation-capability and scan-obligation are the same coin.** You cannot hold
gateway-side takedown AND the "we can't see it, so we can't be expected to scan it"
shield. By choosing a visible gateway, aiko sits in the *scannable, therefore
potentially obligated-to-scan* bucket, not the encrypted-carve-out bucket.

This does not change D1–D4, but it sharpens what "future CSAM tooling" would cost and
why it is separately scoped: if a regulator ever holds a small aiko island to the DIS
Standard's proactive-detection tier, the encryption escape hatch is closed by our own
architecture — *for as long as the gateway sees plaintext*. Which is exactly the
assumption the next section puts under pressure.

### E2EE vs. gateway moderation — a live fork, not a hypothetical

The paragraph above assumed a plaintext gateway is settled. It is not. **E2EE is a
roadmap item** — app-side design item **#25**, tied to islands (#24) and explored
through a Veilid crossover (`aiko_chat_app/docs/design/11-veilid-crossover.html`,
`07-notifications-federation-ready.html`, both flagging that with E2EE "the relay and
the home gateway can't read" message bodies). It is an exploration, not a scheduled
build — but it is on the roadmap, and it collides head-on with the moderation model
this note is built on.

First, dissolve a conflation. The gateway's *existing* crypto is **signing, not
encryption**: the `signing_keys` roster, the message-signing envelope, sovereign-signing
(#1816) authenticate *who wrote a message* and prove *it was not tampered*, while
leaving the body **readable by the gateway**. That is *why* takedown and retraction
work today. **E2EE (#25) is confidentiality** — it hides the body from the gateway.
Signing and encryption are orthogonal; aiko has committed hard to the first and only
flirted with the second. The collision is specifically: signing stays, encryption
arrives.

And it is a genuine collision, because **moderation-capability, scan-obligation, and
E2EE are one axis, not three** — you pick a point on it *per channel*:

- **Plaintext gateway** (today): gateway reads bodies → **can moderate** (takedown /
  retraction / report queue all work) → but is *scannable* → **on the hook to scan**,
  no encryption carve-out.
- **E2EE gateway** (#25): gateway cannot read bodies → **cannot moderate** (gateway-side
  takedown and retraction structurally break — the home gateway can't read what it is
  asked to retract) → but is *unscannable* → **gets the legal carve-out**, the "we
  can't see it, so we can't be expected to scan it" shield.

The same property — gateway visibility — is what grants moderation AND what forfeits
the legal shield. **E2EE does not resolve the plaintext-visibility tension; it walks to
the other end of it.** You trade "I can moderate but I must scan" for "I can't moderate
but I'm shielded." On a *single channel* you cannot have both server-side takedown and
end-to-end confidentiality. This is, arguably, the real fork the whole moderation
design has been circling.

Two honest resolution shapes, neither free (neither is decided here):

- **Per-channel exclusivity** — a channel is *either* moderatable-and-scannable (plaintext,
  gateway-moderated) *or* E2EE-and-shielded-but-unmoderatable. You never get both on one
  channel, and the island advertises which a channel is.
- **Membership-based moderation in E2EE channels** — the retraction becomes a signed
  *member* action other clients honour, not a gateway action (exactly the Veilid framing
  in #11: "the bot is a member with a keypair, not a server reading"). This keeps *some*
  moderation without gateway visibility, but loses server-side takedown and any scanning
  entirely, and pushes the whole trust model client-side.

This is the deepest unresolved question in the design, and it is upstream of D1–D4 (which
hold regardless): D1 governs *which islands* federate; this governs *whether the gateway
can see content at all*, which decides whether within-island moderation and the legal
posture even exist in their current form. It binds directly to the open "what is aiko,
given VeilidChat is the reference sovereign E2EE chat?" question in
`aiko_chat_app` design #11.

### Who is on the hook: operator vs. author (and the content-blind-infra rule)

Everything above is written about the **operator** — the person running an island.
That is deliberate, but it hides a distinction that matters enormously as aiko moves
from "two islands I run" to "a protocol strangers run":

- **Today, the aiko project author is also the operator.** `chat.imagineering.cc`
  and `chat.enspyr.co` run on boxes we control. For those islands there is no
  author/operator gap — every operator obligation in this note lands on us directly.
  This is the normal case and the reason the runbook is not hypothetical.
- **In the federation future, a stranger running our code is the operator, not us.**
  The question then becomes whether the *software author* is liable for content a
  third party hosts. On the research, that hook is **much weaker**, for a structural
  reason: every offence is a **verb the operator does and the author does not** —
  *transmit / publish / make-available* (Criminal Code s 474.22), *possess or control
  data* (s 474.22A), *host* content, be the *"provider"* of a service. An author who
  ships a neutral, moderation-capable tool does none of those; they never touch the
  content. Authorship of a general-purpose tool is not operation of a service. (The
  narrow exception is *inducement* — actively designing-for or encouraging the illegal
  use — which a moderation-first tool is the structural opposite of.)

**The trap in the middle is a design decision, not a legal accident.** The moment the
project author runs *shared infrastructure other islands depend on* — a registrar, a
directory, a discovery seed, a relay — they become the operator **of that component**.
And whether the operator hooks reattach turns on one property:

> **The content-blind-infra rule: any shared infrastructure the author runs must
> touch only *metadata* (island X exists at address Y), never *content* (a cached or
> relayed message).** A directory that *lists* islands keeps the author clear; a
> directory that *relays their traffic* makes the author an operator of a
> content-carrying service, with every hook in this note attached.

This is not a new constraint invented here — it is the liability reading of an
existing architectural commitment ([`concept_directory_discovery_bootstrap_not_centralization`]:
every gateway is a full directory, discovery is plural not central; and the HyperSpace
layer is a *service-graph, not a datastore*). Keeping shared infra content-blind was
always the elegant choice; it turns out to also be the line that keeps the author out
of operator liability. Elegance and legal exposure point the same way — which is the
kind of coincidence worth trusting.

Caveat, honestly: the operator side of this line is exercised law; the author side is
more settled (a neutral tool's author is generally not the operator), but the *exact*
point at which running discovery/registrar infrastructure tips the author into
operator is untested and design-dependent. That boundary is a specialist question,
not something to assert from this note (see Open questions).

### Risks and trade-offs

- **Allow-list peering kills frictionless federation.** Named in D1. Mitigated by
  keeping user-registration openness separate from peer-openness, and by the fact
  that the mesh was always going to be a deliberately-woven trust graph
  ([`06-identity-and-trust`]), not an open graph.
- **The runbook depends on an untested definitional question.** Every AU obligation
  below turns on whether a solo self-hosted node is a "provider" / "Australian
  hosting service provider". The statutory definitions appear to capture it on their
  face, but this has never been tested against an individual operator (see Open
  questions). The runbook is therefore written *assuming the answer is yes*, because
  that is the safe assumption when the downside is a 15-year offence.
- **No shared detection tooling exists for a node our size.** IFTAS discontinued its
  Content Classification Service *and* FediCheck denylist automation in March 2025
  for lack of funding [IFTAS 2.0 post, 2025-03]. PhotoDNA access is gated to
  registered entities a solo operator cannot join. So there is currently *no* funded,
  legal, off-the-shelf CSAM-detection option for a small operator — detection is a
  genuinely open hole, which is another reason D1 (never admit the content) beats any
  detect-after-admit strategy.
- **Reactive posture still has an exposure window.** Even with D1, a *trusted* peer
  can turn bad, or a local user can post CSAM. The runbook, not the allow-list, is
  the control for that case. The allow-list shrinks the attack surface; it does not
  eliminate it.

### The legal picture, side by side

Verified summary of both research passes. **Not legal advice**; figures and section
numbers are cited for a lawyer to check.

| Question | United States | Australia |
|---|---|---|
| Broad intermediary immunity (§230-style)? | Yes for most claims (§230), **but federal criminal CSAM law is carved out** (§230(e)(1)) — no shield there | **No.** No §230 equivalent. Only a narrow mere-conduit/caching exemption (BSA 1992 Sch 5 cl 91) that does **not** reach hosts of user content [ALRC] |
| Proactive scanning required? | **No** — 18 U.S.C. 2258A(f) bars construing any scan requirement | Not universally, **but** the DIS/RES Industry Standards impose proactive detect-and-remove of *known* CSAM on hosting/RES/DIS with **no size exemption** [eSafety] |
| Mandatory reporting trigger | Report to NCMEC CyberTipline on **actual knowledge** (2258A) | Refer to the **AFP** when a host is *aware* its service can access material it has reasonable grounds to believe is CAM (Criminal Code s 474.25; failure = 800 penalty units) |
| Criminal mens rea for transmit/possess | **"Knowingly"** (2252) — genuinely unwitting relay is not the offence; willful blindness counts | **Recklessness** for material-being-CAM (s 474.22), absolute liability on the carriage-service element; possession (s 474.22A) carries a **reverse legal burden** |
| Intermediary/transient-cache defence | Rests on the "knowingly" fault element, not a statutory safe harbour | **None** in the s 474.24 defence list; protection for a truly unaware relay lives only in the recklessness fault element (fact-specific, untested for automated relaying) |
| Regulator takedown power | Via NCMEC/LE process | **eSafety removal notice, 24-hour window** (OSA s 109); non-compliance 500 penalty units = **$165k individual / $825k body corporate** (s 111) |

The through-line: **Australia is materially harsher than the US** for an operator
our size — lower fault threshold (recklessness, not knowledge), a reverse burden on
possession, no broad safe harbour, proactive-detection standards with no size
exemption, and a regulator that can serve a 24-hour removal notice directly. Since
both live islands and the operator are in Australia, **AU is the governing case**
and Part B is written AU-primary.

---

## Part B — Operator runbook: illegal content on your island

**Read this before you need it. This is a procedure, not legal advice — engage a
lawyer now, not during an incident.** It assumes the worst-case (that you *are* a
"provider"/"hosting service provider" in law) because that is the safe assumption
when the downside is criminal.

The pipeline below is the IFTAS-codified operator playbook [IFTAS
about.iftas.org/library/csam], adapted to Australian recipients and timelines.
IFTAS's framing: *"there are no mitigating circumstances for the presence of
CSAM"*, and you are liable wherever the content is available to end users.

**On discovery of suspected CSAM (do these in order, fast):**

1. **Do not view, copy, download, or forward it beyond what already happened.**
   Every additional handling is potential additional exposure. Do not "gather
   evidence" by saving copies to look at.

2. **Preserve, do not delete-yet.** Secure the material and its associated data
   (account, timestamps, source island, message/ULID) in place, in a forensically
   sound manner, so it is available to law enforcement. Deleting before you have
   preserved and reported can destroy evidence; making it *inaccessible to users* is
   step 4 and is different from destroying it. (This ordering — preserve, report,
   *then* make inaccessible — is IFTAS's; confirm it with your lawyer, because
   "possession" duration and "preservation" duty pull in opposite directions and the
   balance is jurisdiction-specific.)

3. **Report to the AFP / ACCCE immediately.** Australia's mandatory referral duty
   (Criminal Code s 474.25) is triggered by awareness — you are now aware.
   - Australian Centre to Counter Child Exploitation (ACCCE): report via
     `accce.gov.au` (the national reporting hub, AFP-led).
   - This is the AU analogue of the US NCMEC CyberTipline duty. It is
     awareness-triggered, and once triggered, failure to refer is itself an offence.

4. **Make it inaccessible to users — immediately.** Execute a takedown/retraction on
   the message so no client can render it (the #7 retraction path). For a CSAM
   incident specifically, this must also **purge the stored plaintext and any cached
   media** from the gateway once preservation (step 2) and the AFP referral (step 3)
   are satisfied — "inaccessible to users" and "not in your possession" are separate
   legal tests, and s 474.22A reaches *possession/control of stored data*.

5. **Permanently ban the account and, if the content came from a federated peer,
   remove that peer from the allow-list (D1).** A trusted peer that emitted CSAM has
   falsified the trust that admitted it.

6. **If eSafety serves a removal notice, the clock is 24 hours** (OSA s 109). You
   will already have done step 4; the notice adds a formal deadline and a
   non-compliance penalty (s 111). Respond and document compliance.

7. **Cooperate fully with any investigation.** Retain the preserved material and
   logs per law-enforcement direction; do not destroy them on your own timetable once
   an investigation is live.

8. **Tell your hosting provider only as your lawyer advises.** Providers will act on
   their own ToS (and may cooperate with LE regardless of your crypto sovereignty) —
   this is a real lever at the physical layer, but sequencing the disclosure is a
   legal call.

**Standing preparation (before any incident):**

- Adopt D1 (allow-list peering) so untrusted islands never have a path to your node.
- Keep the AFP/ACCCE reporting URL and your lawyer's contact in the deploy runbook,
  not in your memory.
- Know your answer to "am I a provider/hosting-service-provider under the OSA and
  Criminal Code?" — get it from a lawyer once, in writing, because every obligation
  above hinges on it.

### Open questions

1. **The definitional question that governs everything: is a solo, non-commercial,
   self-hosted aiko island a "provider of a designated internet service" (OSA s 14)
   and an "Australian hosting service provider" (Criminal Code s 474.25)?** The
   statutory definitions are function-based and appear to capture it, but this is
   untested against an individual operator. This is a **one-time lawyer question**
   and it is the highest-value thing to resolve. Everything in Part B assumes "yes".

1a. **The author/registrar boundary (companion to Q1): where does running federation
   infrastructure tip the software author into being an "operator"/"provider"?** If
   the project author runs a registrar, directory, discovery seed, or relay, at what
   point do the operator hooks attach — and does the content-blind-infra rule
   (metadata-only, never content) actually hold the line in law, or is *listing* an
   island that turns out to host CSAM enough to draw exposure? Same specialist as Q1.

2. **Does the DIS Industry Standard's proactive detect-and-remove tier actually bind
   a small island, and at which tier?** The Standard has no size exemption and
   defaults un-assessed services toward higher obligation, but application to a
   hobbyist node is untested. If yes, and given the closed encryption carve-out (our
   gateway sees plaintext), what is the minimum-viable detection posture — and is any
   legal CSAM-hash source available to a non-registered operator at all?

3. **Preserve-vs-purge timing.** Steps 2 and 4 pull against each other (evidence
   preservation vs. minimising possession duration). What ordering does an Australian
   lawyer actually endorse?

4. **Does D1's allow-list belong at the registrar, the gateway, or both?** The mesh
   has multiple federation entry points; the single-door principle says seal the
   mutator, not each caller. Which component owns the peer allow-list is a spec-delta
   question for after agreement.

5. **Should a CSAM retraction be a distinct event kind from an ordinary takedown
   retraction?** The user-visible effect is identical (monotonic remove), but the
   operator obligations (purge plaintext, refer to AFP, preserve) differ. This may
   argue for a separate incident path rather than reusing the ordinary takedown UI.

6. **The E2EE fork (the deepest one): does aiko keep a plaintext, moderatable gateway,
   adopt E2EE (#25) and move to membership-based moderation, or run both per-channel?**
   Server-side takedown/retraction and end-to-end confidentiality cannot coexist on one
   channel. This decision sits upstream of everything else here and of the "what is aiko
   vs VeilidChat" question in `aiko_chat_app` design #11. It is an architecture call, not
   a legal one — but it flips the legal posture (E2EE moves aiko into the encryption
   carve-out at the cost of gateway moderation).

   **A "third point on the axis" was investigated and rejected** (crucible
   `docs/crucible/08-confidential-moderation/`, 2026-07-30): an operator-blind
   TEE/enclave LLM moderator that reads E2EE plaintext inside a hardware cage and emits
   only signed verdicts, aiming for confidential-AND-moderatable. **Invalidated at
   research:** a TEE does not protect a key from a motivated *host* with physical
   access (BadRAM ~$10, TEE.Fail <$1k both extract keys and forge attestation on
   SEV-SNP/TDX; vendors put physical attacks out of scope), credible attestation is a
   large-org capability a solo self-hoster can't reproduce, and colluding parties defeat
   any content-readable detector by layering their own encryption ("any scheme that can
   detect the content could break encryption generally"). **Takeaway for this fork:
   confidential-from-a-motivated-adversary AND readable-by-a-moderator is likely close
   to information-theoretically impossible — the axis above is a *true* dichotomy, not a
   line with a buildable midpoint.** So question 6 is a genuine either/or, not a
   both-and awaiting cleverer crypto.

   **RESOLUTION DIRECTION (decided 2026-07-30): make the either/or an explicit operator
   election.** Rather than the project picking a side, each **island operator elects its
   mode** — **E2EE** (client-encrypted channels the gateway cannot read: genuine
   inability, legal carve-out, but no gateway moderation; relies on client-reporting +
   metadata) **or Moderator** (plaintext gateway: the shipped takedown/retraction works,
   but on the hook to scan, no shield). Sovereignty made concrete: the node that bears
   the legal risk picks its point on the axis and owns the consequence. Full design in
   crucible `docs/crucible/09-operator-mode-election/` (→ design note 08 if it survives
   temper). Sub-decisions carried there:
   - **"Moderator" is a commitment, not a capability** — kill the dangerous middle
     (plaintext-but-not-moderating = liability without the safety work). Holding
     plaintext commits you to moderate (report queue + CSAM runbook on).
   - **Per-island granularity — DECIDED (2026-07-30): aiko is one-community-per-island.**
     An island is a community with one posture, so per-island mode is both the liability
     atom and the encryption atom (they coincide). A different posture = a different
     community = a different island, by design. This validated per-island against a
     cross-family design temper (crucible 09) which had flagged per-island as possibly
     the "wrong encryption atom" for a multi-community-per-box product — a shape aiko has
     now explicitly rejected.
   - **The peer allow-list (D1) becomes mode-aware** — a Moderator-island may refuse to
     federate with E2EE-islands (can't inspect inbound content it is legally liable for),
     or vice versa. Mode is a federation-trust property, not just local config.
   - **Legibility:** the mode must be visible/verifiable to users before they speak.
     Lucky asymmetry — client-side E2EE is self-verifying (the client knows it handed the
     server only ciphertext), so no enclave-style attestation is needed.

## What we do **not** claim

Two claims were tested and **refuted** by the verification pass; they are excluded
from the design and recorded here so they are not silently reintroduced:

- **Not** that OSA removal notices have unlimited "anywhere in the world if the
  victim is Australian" extraterritorial reach — the 2024 *eSafety v X Corp*
  litigation actually qualified that geographic reach. (This does not help aiko much:
  the operator and both islands are *in* Australia, so domestic reach is not in
  question.)
- **Not** that Australia has no mandatory provider-reporting duty — s 474.25 does
  impose one on hosts, awareness-triggered.

Additionally under-verified and flagged as such: the EU position (DSA hosting safe
harbour + the pending CSAM-scanning regulation) was thin in the first pass and is
**not** relied on here; if an island is ever hosted in the EU, that is a fresh
research pass.

## References

**Primary — United States**
- 18 U.S.C. 2258A (provider reporting; 2258A(f) no-scan) — law.cornell.edu/uscode/text/18/2258A
- 18 U.S.C. 2252 (knowingly transport/receive/distribute) — law.cornell.edu/uscode/text/18/2252
- IFTAS operator CSAM guidance — about.iftas.org/library/csam
- IFTAS 2.0 rescoping (CCS + FediCheck discontinued, 2025-03) — about.iftas.org/2025/03/17/iftas-2-0-rescoping-and-refocusing
- Matrix, "Building a Safer Matrix" (2025-02) — matrix.org/blog/2025/02/building-a-safer-matrix
- Matrix, "Introducing Policy Servers" (MSC4284, 2025-04) — matrix.org/blog/2025/04/introducing-policy-servers
- Mastodon `LIMITED_FEDERATION_MODE` — docs.joinmastodon.org/admin/config

**Primary — Australia**
- Online Safety Act 2021 (Cth) — legislation.gov.au/C2021A00076/latest (s 109 removal notice, s 111 penalty, ss 13/13A/14 service definitions)
- Criminal Code Act 1995 (Cth) Div 474 — ss 474.22, 474.22A, 474.24 (defences), 474.25 (mandatory AFP referral); UNODC SHERLOC reproduction + legislation.gov.au/C2004A04868/latest
- eSafety Compliance & Enforcement Policy (Oct 2024) — esafety.gov.au
- eSafety DIS Industry Standard fact sheet (2024) + industry codes/standards register — esafety.gov.au/industry
- ALRC on the absence of a §230-style safe harbour — alrc.gov.au

**Internal**
- [`02-bus-decouple-and-islands`], [`06-identity-and-trust`]
- Concepts: [`concept_federation_fork_key_as_identity`],
  [`concept_directory_discovery_bootstrap_not_centralization`],
  [`concept_sovereignty_scoped_moderation`],
  [`concept_add_remove_asymmetry_never_hide_a_hide`],
  [`concept_island_is_mesh_not_broker`]
- #7 / PR #104 (forward-ULID retraction — the make-inaccessible primitive)
