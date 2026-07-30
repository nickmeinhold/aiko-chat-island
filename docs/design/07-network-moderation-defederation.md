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
architecture. (The counter-move, if it ever came to that, is genuinely hard: E2EE
that hides content from the gateway would break the very moderation model we shipped.
That trade is not resolved here; it is flagged as the deepest tension in the design.)

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
