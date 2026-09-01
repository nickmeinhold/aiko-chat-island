# RESEARCH — prior art for "auditable by construction, not reputable"

Heat movement, `/crucible` auditable-island. Adversarial brief: findings that refute
the thesis lead. Every claim tagged **[DEPLOYED]** (measured/shipped fact),
**[PAPER]** (a claim in the literature, not deployed at scale), or **[INFERENCE]**
(mine, unverified).

---

## HEADLINE: the thesis is refuted at Q1 in its strong form, and badly damaged at Q2

Every deployed transparency system I could find **does** make a comparative /
admission judgement — not as a *ranking* of quality, but as a **curated
membership list of who is allowed to be a proof-source**, shipped in the client
and maintained by a central authority using explicit discretion. And the one KT
protocol whose security actually scales with observer count (gossip) needs
thousands of clients before it detects anything. A two-island federation has
neither the admission-free property nor the density.

The thesis survives only in a weaker, still-useful form — see the closing
section.

---

## Q1 — Absolute vs relative: is any client check a ranking? (THE CRUX)

**Finding 1a: Chrome's CT policy IS an admission/reputation layer for logs.
[DEPLOYED]**

The client-side check itself is binary-per-SCT, but the *set of acceptable
proof-sources* is a curated trust list, and the policy explicitly demands
**operator diversity** — a relational property across sources, not an absolute
one:

> "At least one Embedded SCT from a CT log that was `Qualified`, `Usable`, or
> `ReadOnly` at the time of check" … "There are Embedded SCTs from at least N
> distinct CT logs" (N=2 for certs ≤180 days, N=3 above) … **"at least two SCTs
> must be issued from distinct CT log operators as recognized by Chrome"**
> — https://googlechrome.github.io/CertificateTransparency/ct_policy.html

"…as recognized by Chrome" is the fatal phrase. The client cannot evaluate a
never-before-seen log's proof at all. Admission is discretionary:

> "New operators must assert they are organizationally independent from all
> existing CT log operators." … "Maintain log availability of 99% or above"
> (90-day rolling, per-endpoint) … **"The decision to remove logs is made at the
> Chrome team's discretion based on all information available"** — considering
> "the root cause of the incidents, the log operator's response, and the impact."
> — https://googlechrome.github.io/CertificateTransparency/log_policy.html

So: uptime SLAs, an independence assertion, a human incident-response judgement,
and an at-discretion removal power. **That is a reputation system for logs.** It
is exactly the thing the thesis says a by-construction design eliminates, and CT
— the canonical by-construction system — has one, deployed, at global scale.

**Finding 1b: Signal's brand-new KT deployment hardcodes a named auditor
quorum. [DEPLOYED, Aug 2026]**

> Clients "require signatures from each of three auditors: one operated by
> Signal, one operated by Cloudflare, and one operated by Trail of Bits."
> — reported of https://signal.org/blog/automatic-key-verification/ ; the blog
> itself names Cloudflare and Trail of Bits as "independent auditors of these two
> types of trees", and says auditors "will only ever sign each page and index
> book once", so "Alice and Bob can be assured that the clerk is maintaining
> exactly one ledger."

The client check is a signature-threshold against **specific named
organisations**. Not a ranking — but unambiguously a *who-do-we-trust* list, made
by Signal, shipped in the app. Note also what the blog does **not** say: what
happens if an auditor is unavailable, or if two auditors disagree. **[INFERENCE]**
An unavailable auditor almost certainly fails open (or the feature degrades),
because failing closed would break messaging — which is the same
"nobody-actually-enforces" hole as Q5.

**Finding 1c: the honest generalisation.** Across CT, Signal KT, WhatsApp AKD and
the Go checksum database, the shape is identical: **the cryptographic check is
absolute; the trust anchor is an admission decision made out-of-band.** The Go
sumdb is the cleanest case — a single public key baked into the toolchain: no
ranking, but also no federation and no choice. **[DEPLOYED]**

**What this means for the thesis.** "Auditable by construction" does not dissolve
trust decisions; it *relocates* them from "is this operator behaving?" (continuous,
subjective, whitewashable) to "is this operator admitted as a proof-source?"
(one-time, structural). That relocation is real and valuable. But the claim
"there is nothing left to be reputable about" is **false as stated** — every
deployed system kept an admission layer, and Chrome's is explicitly
discretionary. Expect the Temper movement to hit this hard; better to concede it
now and defend the weaker claim.

---

## Q2 — Small-N viability: this is the make-or-break, and the numbers are bad

**Finding 2a: gossip detection is a density property, and the only quantified
deployment study is three orders of magnitude larger than this federation.
[PAPER]**

MINGLE (eprint 2026/1010, "Signal and Ready to MINGLE: In-Band Gossip for Key
Transparency Split-View Detection in E2EE Messengers"):

> "MINGLE yields evidence of a targeted split view in a **12000-client**
> deployment within about 5 minutes when only 20% of clients participate and
> gossip is attached to roughly 5% of messages."
> — https://eprint.iacr.org/2026/1010

There is no threshold below which it stops working — it degrades continuously —
but detection is a function of **cross-partition communication events**. With two
islands and a handful of users, the expected time-to-a-cross-partition-gossip-event
is the binding quantity, and nothing in the literature suggests it is short.
**[INFERENCE]** At N=2 the gossip graph is a single edge: an operator who wants to
equivocate needs to isolate exactly one peer, which is trivially achievable and
indistinguishable from that peer being offline.

MINGLE also names a limitation that lands directly on a small federation:

> "equivocation that begins at registration evades immediate detection, though
> the append-only log ensures it remains retroactively exposable once any
> cross-partition gossip event occurs."

**[INFERENCE]** In a 2-island system where users mostly talk within their own
island, "any cross-partition gossip event" may be rare or never. Retroactive
exposability that nobody exercises is not a security property.

**Finding 2b: the honest reframe of small-N.** At N=2, gossip-based
non-equivocation is close to theatre — *but* the same is not true of the
**single-island absolute checks** (append-only consistency proof, inclusion
proof, SMT-style merge-deadline enforcement). Those are self-contained: one
client and one island are enough to catch a log that rewrites history or breaks
its own promise. **[INFERENCE, but structurally sound]** The split is:

| property | needs N observers? | viable at N=2? |
|---|---|---|
| append-only / no-rewrite (consistency proof) | no — one client suffices | **yes** |
| inclusion of my own binding | no | **yes** |
| merge-deadline promise kept (SMT/MMD) | no | **yes** |
| **non-equivocation across parties (split view)** | **yes, density-dependent** | **no** |
| what code the island runs | see Q6 | mostly no |

The design should say this table out loud rather than claim uniform
auditability. A design that quietly implies split-view protection at N=2 is
selling a guarantee it does not have.

---

## Q3 — Does an auditor class reappear? Yes. Everywhere. [DEPLOYED]

CONIKS's founding claim is auditor-free self-monitoring — "users and providers
can collectively audit providers for non-equivocation … downloading a constant
**2.5 kB per provider per day**" (https://eprint.iacr.org/2014/1004.pdf).

Every production descendant added a third party anyway:

- **WhatsApp AKD** → Cloudflare's **Plexi** auditor. The auditor's job is stated
  as "two main guarantees: that epochs are globally unique, and that they are
  valid", and crucially "global uniqueness requires **consistency on whether an
  epoch and its associated root hash has been seen**"
  (https://blog.cloudflare.com/key-transparency/). That is precisely the
  property self-monitoring **cannot** provide: a client sees only its own view.
- **Apple CKV** → "We plan to share more details about our public auditing
  strategy in 2024" (https://security.apple.com/blog/imessage-contact-key-verification/).
- **Signal** → three-auditor quorum (1b).

**Direct answer to the question asked:** yes, "no auditor class" has become
"trust Cloudflare" (and Trail of Bits, and Signal itself). The auditor exists to
supply **cross-client agreement**, which is the same thing gossip supplies —
i.e. the auditor is the *centralised substitute for density*, which is exactly
what a 2-island federation lacks. **[INFERENCE]** For this island design, that
means the realistic choices are: (a) accept no split-view protection, (b) run/
recruit an auditor and admit a trusted third party, or (c) anchor to a public
bulletin board. Confirming (b)/(c) reintroduces the admission problem from Q1.

Academic escape hatch, **not deployed**: Consistency-or-Die (eprint 2024/879)
claims a protocol that "does not … rely on small committees of known external
auditors, or out-of-band channels, or blockchains", using VRF-selected,
initially-undisclosed user endorsers. **[PAPER]** — its whole mechanism is
random selection from a user population, so it is *also* density-dependent and
almost certainly degenerate at N=2.

---

## Q4 — Apple's Signed Mutation Timestamps [DEPLOYED]

From https://security.apple.com/blog/imessage-contact-key-verification/ :

- SMTs are "**auditable promises to make changes to the map**", which make
  "device keys immediately verifiable, thereby maintaining the instant usability
  of iMessage." Structurally the analogue of a CT SCT: sign now, merge later.
- Client-enforced deadline: "**the Messages app verifies that SMTs are merged to
  the map within a 48-hour Maximum Merge Delay.**"
- Client also "verifies inclusion proofs" and verifies "consistency of critical
  append-only logs … right on the user's device."
- Apple *does* gossip, in-band: "Messages also gossips log hashes — by including
  them in the encrypted part of a **small percentage of messages** — with other
  iMessage clients and verifies the consistency of log hashes received via
  gossip." (Note: this is the pattern MINGLE formalises — and it is the pattern
  that needs density.)

**Gaps the primary source does not answer** (flagging rather than inventing):
what exactly is inside an SMT, what the client does when the promise is broken,
and the offline case. **[INFERENCE]** Offline-past-MMD is the interesting one: a
client that was not online to check cannot retroactively distinguish "merged
late" from "merged only after I asked", so the enforcement is only as good as
client liveness — a serious problem for a chat client that may be closed for
days.

---

## Q5 — Failure modes already hit [DEPLOYED]

From Andrew Ayer's catalogue (https://www.agwa.name/blog/post/how_ct_logs_fail),
eight real CT log failures: excessive downtime (2016); **private key reused with
a test log**, producing apparent split views (2016); **append-only violated by a
botched backup restore / database rollback** (2017); operator simply vanished
(2017); two logs that **did not include submitted certificates** (2018); presumed
key compromise via a Salt vulnerability (2020); and a **single hardware bit flip**
corrupting entry 65,562,066 in Yeti2022 (2021).

Three lessons that transfer directly:

1. **The most common failure is operational, not adversarial** — restores,
   reused keys, dead operators, cosmic rays. A hobbyist ARM box will hit these
   *more* often than Google did. A design that treats "log broke its own
   invariant" as an attack signal will produce mostly false accusations.
2. **Detection came from dedicated monitors** (Cert Spotter comparing computed
   root hashes against published STHs), i.e. an auditor class again — not from
   ordinary clients.
3. **CT tolerates failure by redundancy across operators**: "a single log failure
   can't cause a certificate to stop working" because policy requires multiple
   SCTs from different logs. **[INFERENCE] A 2-island federation has no
   redundancy to spend**, so it must either fail-open (and the check is decorative)
   or fail-closed (and one bit flip takes the island offline). This is a real
   design fork the Cast movement must price.

**Why CT gossip was never deployed [DEPLOYED, drafts expired 2018].**
`draft-ietf-trans-gossip-05` expired 18 Jul 2018. Its own text names the killer:
the Trusted Auditor Relationship means "an HTTPS client is providing its
browsing history to a third party"; STH Pollination needed Proof Fetching done
"in a privacy preserving manner" that nobody built; and it required server-side
deployment that "some percentage of HTTPS servers would not deploy."
**Gossip died of a privacy/deployment deadlock, not of a crypto flaw.** The same
deadlock applies here: gossiping who-you-talked-to across islands is a metadata
leak, and this project already has a recorded ruling that the island should learn
neither who is friends with whom nor who is calling.

---

## Q6 — Attestation of RUNNING code: mostly theatre for a hobbyist box

**SLSA does not answer this question at all. [DEPLOYED spec]** From
https://slsa.dev/spec/v1.0/levels : L1 provenance is "trivial to bypass or
forge"; L2 requires a hosted build platform and "prevents tampering *after* the
build"; L3 hardened builds resist "insider threats, compromised credentials, or
other tenants." SLSA is about **how an artifact was built**, not what executes in
production.

So the ladder splits cleanly:

- **Achievable, cheaply, and genuinely falsifiable:** *reproducible builds +
  transparent build log*. A third party rebuilds the git sha and gets the same
  image digest; the digest is logged. This converts "the island says it runs sha
  X" into "anyone can confirm sha X produces digest D" — but it still does not
  prove digest D is what is running.
- **Achievable with effort:** signed provenance at SLSA L2/L3 via GitHub Actions
  (this repo already builds multi-arch images in CI), plus publishing the digest.
- **NOT achievable on a random self-hosted ARM box:** proving the *running*
  process matches the digest. Remote attestation needs a hardware root of trust —
  measured boot extending PCRs from a CRTM, verified by a remote appraiser
  (e.g. Keylime). The operator physically owns the box, and **the operator is the
  adversary in this threat model**. Even with a TPM, an operator with physical
  access and a self-signed EK has no attestation an outsider can trust; you would
  need a manufacturer-rooted EK cert and a verifier the operator does not
  control — i.e. Q1's admission problem, in silicon. **[INFERENCE, high
  confidence]**

**Verdict on the "what code is running" row: it is theatre as currently
conceived** (self-asserted sha at `/health` — the island says what it claims to
run, and a lying operator lies here first and cheapest). It becomes *partly* real
with reproducible builds + a public digest log, which upgrades it from
"unfalsifiable" to "falsifiable by anyone who can also observe behaviour". It
does not become real in the strong sense without hardware the operator does not
own.

---

## What survives

Not "there is nothing left to be reputable about." The defensible claim is
narrower and should be stated at exactly this scope:

> **Every check that a client can make against a single island — append-only
> history, inclusion of its own bindings, and a signed promise with a
> client-enforced deadline — is absolute and works at N=1. Non-equivocation
> across islands is density-dependent and does not work at N=2 without either an
> auditor or a public anchor. What code an island runs is not attestable at all
> without hardware the operator does not control.**

Three rows of the decomposition are real; one needs an admitted third party; one
is currently theatre. Design against that table, not against the slogan.

## Sources

- https://googlechrome.github.io/CertificateTransparency/ct_policy.html
- https://googlechrome.github.io/CertificateTransparency/log_policy.html
- https://signal.org/blog/automatic-key-verification/
- https://blog.cloudflare.com/key-transparency/
- https://security.apple.com/blog/imessage-contact-key-verification/
- https://eprint.iacr.org/2014/1004.pdf (CONIKS)
- https://eprint.iacr.org/2026/1010 (MINGLE)
- https://eprint.iacr.org/2024/879 (Consistency-or-Die)
- https://www.agwa.name/blog/post/how_ct_logs_fail
- https://datatracker.ietf.org/doc/html/draft-ietf-trans-gossip-05
- https://slsa.dev/spec/v1.0/levels
- https://engineering.fb.com/2023/04/13/security/whatsapp-key-transparency/
