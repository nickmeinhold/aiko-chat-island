# 🜂 RESEARCH — Heat

> Movement 2. Bounded inline research pass (WebSearch/WebFetch + repo grounding), 2026-08-25.
> Labelled **shallow-to-medium**: no background researcher fleet was spawned. External prior art
> is real and cited; repo grounding is first-hand.
>
> **Split, per the skill's contract:** *constraints* and *others' failure modes* go forward to
> Spark. The *solution space* (§3) is **withheld from Spark** and released at Cast, so the
> out-breath cannot recombine the cached answer.

---

## 1. The crux, stated precisely

> Can the island **verify** a bilateral tie (to authorize a ring) without being able to
> **enumerate** one (which would create the queryable social graph ADR-0004 forbids and island
> Design 05's C5 depends on not existing)?

Verification and enumeration are different powers, and the whole design lives in the gap between
them. Everything below is aimed at whether that gap is real or imaginary.

---

## 2. Constraints and others' failure modes → **forwarded to Spark**

### 2.1 Signal: private contact discovery needed *hardware*

Signal's stated design goal matches ours almost word for word — *"the Signal service's design does
not depend on knowledge of a user's social graph in order to function, and if you trust the
Signal service to be running the published server source code, then the Signal service has no
durable knowledge of a user's social graph if it is hacked or subpoenaed."*

And their stated obstacle is the one that matters to us:

> *"for the server not to learn anything about the query, it will need to at least touch every
> possible record in its data set when calculating a response."*

Their answer was **SGX enclaves** with a reproducible `MRENCLAVE`, plus a custom ORAM layer to
stop access-pattern leakage. **An island cannot have this.** A self-hosted box run by a hobbyist
operator has no attested enclave, and even if it did, the operator is the very party the property
would need to exclude. So: *the Signal solution is unavailable to us by construction*, and any
design that quietly assumes server-side privacy-against-the-operator is repeating a claim Signal
needed special hardware to make.

### 2.2 The N² problem — and why small islands make it *worse*, not better

A natural shape is to store the tie as an opaque digest of the pair — the OpenID Connect PPID
construction, `H(k, uid, rid)` with `k` a high-entropy server key. Against an *outside* observer
holding only the DB, that is genuinely opaque.

**It fails against a compromised island, and small N is what kills it.** The island holds `k`
(it must, to compute the lookup) and holds the user roster (it must, to run). So it can compute
all `N²` candidate digests and invert the entire graph by brute force. Our islands run **33 and
13 users** — that is 1,089 and 169 hashes. Milliseconds.

**This inverts the usual intuition and should be stated loudly:** a big social network's hashed
pairs are protected by scale; an island's are not protected at all. Any "we hash the pair"
proposal must clear this specific number, not the general argument.

### 2.3 Mutual contact discovery is a real, named primitive with a literature

Hoepman, *Mutual Contact Discovery* (arXiv:2209.12003) proposes exactly the bilateral shape:

- two users discover each other **only if both independently submit the other's identifier**;
- the server **never receives a complete roster**, so it *"cannot enumerate a user's contacts"*;
- it *"learns only about matches — relationships where both users queried about each other — and
  cannot infer non-matches or non-queried identifiers"*;
- **mutual consent kills one-sided harassment structurally**, not by policy: address-book
  harvesting and unsolicited contact recommendation both stop working.

That last point is worth taking seriously as an *independent* argument for the candidate: mutual
consent is a spam/abuse control, not only a privacy control. This is prior art that the
consented-bilateral-tie shape is a known-good primitive rather than an invention.

**Its stated limitations are our failure modes, inherited:**
- *"An attacker who controls many accounts can still perform broad reconnaissance"* by probing
  many identifiers — i.e. **sybil probing defeats query privacy**. Ties directly to app ADR-0006
  (sybil resistance) and means any submit/probe endpoint needs a rate-limit and a cost.
- *"Requires out-of-band verification"* to prevent account-takeover attacks.
- *"Participants must trust the service provider to not collude with adversaries"* — i.e. the
  honest-server assumption is load-bearing, which is the assumption an island most wants to drop.

### 2.4 Capability tokens: the revocation tax is the whole cost

From the capability-security literature: *"Capabilities are bearer tokens, and anyone who obtains
one gains access. In distributed systems, revocation latency is a concern, so short TTLs and
revocation lists that propagate quickly should be used."* And: *"authorization [must be]
enforceable at runtime, not only at mint time."*

So a grant-as-bearer-token shape does not remove state — it **moves** it, from a friend list to a
revocation list, and buys a new problem (latency between "I unfriended you" and "you stop being
able to ring me"). The privacy-preserving-credentials literature offers revocation lists with
non-membership proofs, which is real but heavy machinery for two islands and 46 users.

### 2.5 Pairwise identifiers as an *unlinkability* tool

PPIDs (`H(k, uid, rid)`) exist to stop *verifier collusion*: *"if an employer receives identifier
X and an insurer receives identifier Y, they cannot match records even if they compare databases,
because they have no common key to join on."* The federation-relevant read: a tie expressed as a
pairwise identifier is **not correlatable across islands**, which matters the moment more than
one island holds ties for the same person. Note this is unlinkability *between* verifiers, and is
orthogonal to §2.2's enumeration problem *within* one verifier.

---

## 3. Solution space — **WITHHELD FROM SPARK, released at Cast**

*(Deliberately fenced. Handing the out-breath a catalogue of existing answers produces
recombination of the cached default instead of an escape from it.)*

The families of answer available: server-side stored pair (plain / hashed / PPID); client-held
bearer grant verified per-use; a pair of signed consent messages carried on the existing message
substrate; mutual-submission matching (Hoepman); enclave-based PSI (excluded by §2.1);
capability-with-revocation-list; and the null option — an app-local contact list with no island
state at all, where the island verifies a presented credential blind.

---

## 4. Repo grounding — two facts that constrain the shape hard

### 4.1 **The gateway is a CARRIER, not a verifier — it has never checked a signature**

`domain/signing.py:9` — *"It never checks the signature itself — verification is the recipient's"*.
`models.py:711` on `SigningKey`, and this is a deliberate design choice with teeth:

> *"UNIQUENESS IS PER-USER, NOT GLOBAL — a deliberate carrier-semantics choice, not an oversight.
> The gateway is a CARRIER, not a verifier: it never checks a signature, so when account A
> presents `pubkey`, all it knows is 'A (authed via its session) used this key', NEVER 'this key
> belongs to A'."*

**Consequence:** the `SigningKey` roster **cannot** be used to verify a friend grant. It records
*observed* key usage, not an authenticated key→account binding. Building admission on it would be
mistaking a carrier for an authority — precisely the confusion its docstring exists to prevent.

### 4.2 **But the island already verifies signatures in exactly the right way — for guardians**

Island Design 05, shipped as migration 0013, states the contrast explicitly and pre-emptively:

> *"Contrast with SigningKey (#1816), deliberately. The signing roster stores a pubkey the gateway
> never verifies against (a carrier, so per-user non-unique, collisions kept as evidence).
> Approver pubkeys are the mirror image: the gateway DOES verify against them, they are
> registered authenticated, and a quorum of them is authoritative-for-takeover (not for
> identity). Same table shape, opposite trust posture — stated so a reviewer doesn't
> pattern-match the carrier invariant onto it."*

**This is the precedent to build on, and it is already in production.** A friend-grant key follows
the *approver* pattern (registered while authenticated, verified against, authoritative for one
specific capability) and **not** the `SigningKey` carrier pattern. The island has both postures
already; the design must say which one it is inheriting, in those words, or a reviewer will
pattern-match the wrong invariant — which the Design 05 author explicitly predicted.

It also gives us domain-separation prior art: approvals are signed over
`"aikochat:recover:v1" ‖ nonce ‖ account_id ‖ credential_id ‖ pubkey`, with the note that the
distinct domain tag makes an approval *structurally un-replayable* into the message-signing path.
A friend grant needs its own tag on the same principle, or a grant becomes replayable into
recovery or vice versa.

### 4.3 The wake path's gate 0 is a constraint on any admission design

`push_service`: a wake can only be scheduled **downstream of an accepted `create_outbound`**,
which is why block/ban/idempotency traverse for free. Its own docstring names the boundary:

> *"A future caller that wakes a device WITHOUT an accepted message behind it would break this
> property and needs its own gate map."*

So a design where "ringing" is a signed message that passes through `create_outbound` inherits
the whole existing gate stack. A design that adds a parallel ring path **owes a fresh gate map**
and re-litigates block, ban, and idempotency from scratch. That is a very large hidden cost and
should be priced in the Cast, not discovered at Temper.

### 4.4 Existing unilateral relations, for contrast

`memberships`, `blocks`, `mutes` — all one-sided. **Note the asymmetry already shipped:** a block
list is island-side state that already tells a compromised island something socially meaningful
(who refuses whom). Whatever C5 exposure a friend graph adds, it is added to a baseline that is
not zero. Worth stating so the C5 argument is priced honestly in both directions rather than
treated as a step from perfect to compromised.

---

## 5. What Heat changed about the candidate

1. **Enumeration-resistance against a *compromised island* is probably unachievable for any
   stored pair representation** at N=33 (§2.2). If the design wants that property, the island
   must store nothing pair-shaped — which pushes toward client-held, island-verified credentials,
   and toward the shape the call-object strike already prescribed (signed messages, not a table).
2. **The island becoming a verifier is a real architectural step, but it is not a new one** —
   Design 05 already did it for guardians and wrote down the trust-posture contrast. The design
   must name which posture it inherits (§4.2).
3. **Mutual consent has an abuse-prevention payoff independent of privacy** (§2.3), which
   strengthens the candidate on an axis the Ore document did not claim.
4. **The honest framing of C5 is a delta, not a cliff** (§4.4) — blocks already leak a social
   signal. My Ore claim stands but should be argued in deltas.

## Sources

- [Signal — Technology preview: Private contact discovery](https://signal.org/blog/private-contact-discovery/)
- [Signal — The Difficulty Of Private Contact Discovery](https://signal.org/blog/contact-discovery/)
- [Signal — Building a Faster ORAM Layer for Enclaves](https://signal.org/blog/building-faster-oram/)
- [Hoepman — Mutual Contact Discovery (arXiv:2209.12003)](https://arxiv.org/pdf/2209.12003)
- [Curity — Pairwise Pseudonymous Identifiers](https://curity.io/resources/learn/ppid-intro/)
- [SpruceID — What Are Pairwise Identifiers?](https://spruceid.com/learn/pairwise-ids)
- [OPPID: Single Sign-On with Oblivious Pairwise Pseudonyms (eprint 2024/1124)](https://eprint.iacr.org/2024/1124.pdf)
- [Capability-Based Security (implementation notes)](https://oneuptime.com/blog/post/2026-01-30-capability-based-security/view)

---

# 6. Heat addendum — E2EE, moderation, and a conflated dial

> Added 2026-08-25 17:32 after Nick pushed on three premises. Two of my earlier readings were
> wrong; the third question opened a finding that is bigger than this candidate.

## 6.1 CORRECTION — the mode election SHIPPED; only the implementation is bolted

I earlier implied per-island E2EE configurability didn't exist. **It does.** `config.py:365`:

```
island_mode: IslandMode = IslandMode.MODERATOR
```

Signed into the island's self-manifest (`GET /v1/island`) so the posture is *"HONEST and LEGIBLE
to clients before a user speaks"* — crucible-09 Phase A, live on both boxes. Two positions,
`moderator` and `e2ee`. What did **not** ship is the E2EE implementation: `e2ee` is
**hard-rejected at boot in every environment**, deliberately —

> *"No encryption exists yet, so advertising it would be the exact mislabel this feature prevents
> (users believing E2EE while the operator reads plaintext)… The value is in the enum only so the
> wire/type vocabulary is forward-stable, **never selectable**."*

**The dial exists, is signed, is legible — and one position is bolted shut.**

## 6.2 How the field reconciles E2EE with moderation: MESSAGE FRANKING

The standard answer, invented at Facebook, shipped in WhatsApp and Messenger Secret
Conversations. The sender's message carries a **franking token** committing to both content and
sender. The recipient's device verifies it. If the recipient reports, they hand over plaintext
plus token, and the platform verifies **"this account sent exactly this"** — having never read
it, and unable to be fooled by a forged report. Unreported messages stay deniable.

**This does NOT contradict [[concept_confidential_xor_moderatable]], and the bundle must not
claim it does.** That kill targeted a **proactive** moderator that can *read* content (the
operator-blind enclave scanner, invalidated at Heat 2026-07-30). Franking grants no read access
to anyone. It makes the **recipient** the sole party who can reveal, and makes their revelation
*verifiable*. The dichotomy stands; franking sidesteps it by changing **who reveals**.

**It also maps onto machinery already shipped here** — the report queue plus the #7
takedown/retraction path are already *reactive and recipient-initiated*. Franking is the
E2EE-compatible version of the flow this island already runs, not a new moderation philosophy.

Active area, not fringe: Meta published on Scam Alert for WhatsApp with E2EE + verifiability
guarantees on 2026-08-12; two SoK papers survey the space.

## 6.3 An SFU is NOT an obstacle to E2EE calls — and LiveKit already ships it

Correcting a second implicit assumption of mine. **LiveKit** — self-hosted on both our boxes —
has **built-in E2EE**: room-level, automatically applied to all media tracks from all
participants, plus data channels, via insertable streams. **The SFU forwards encrypted packets
it cannot decrypt.** Group calls fully supported.

Named trade-offs when E2EE is on: **server-side recording/egress, transcription, and simulcast
layer switching become limited or unavailable.**

## 6.4 THE FINDING — `island_mode` conflates two independent dials

`island_mode` is one flag standing for two orthogonal properties:

| dial | what it governs | what it costs to turn on |
|---|---|---|
| **message confidentiality** | gateway holds plaintext bodies (`models.py:501`, `Text`) | MLS client-side encryption — a large Phase B build, and it breaks today's proactive moderation posture |
| **media confidentiality** | whether the SFU can decrypt call media | **a LiveKit room option.** No MLS. No message-path change. |

**Media E2EE is separable from message E2EE and is available today.** Calls could be
end-to-end encrypted without touching the message path, the report queue, or the moderator
election — the `e2ee` bolt is holding the media door shut for a reason that only applies to the
message door.

**But turning it on silently would be the exact defect `config.py` guards against, inverted.** An
island advertising a signed `moderator` manifest while its calls are actually E2EE is *mislabelled
by omission* — under-claiming rather than over-claiming, but still a manifest that does not
describe the island. So the honest version is **two dials, two signed manifest fields**, not one
flag quietly acquiring a second meaning. That is a schema/wire change to a signed artifact and
therefore not a side effect anyone should ship inside another feature.

## 6.5 Consequence for THIS candidate

1. The friends design now sits on a call path that **could plausibly be E2EE**, and the design
   must not assume the island can see call media or call content.
2. **An agent that participates in a call must be a KEYHOLDER, not an eavesdropper.** Under media
   E2EE a pipeline that cannot decrypt cannot run inference — which collides with the `webrtc://`
   DataScheme thread (#2828/#3157) where aiko pipelines *consume* media. Arguably the right
   answer (an agent participates rather than wiretaps), but it is a constraint nobody has stated,
   and it is the same question as the agent-identity thread approached from the other side.
3. Franking is the moderation shape that survives if any of this becomes confidential — and it is
   **reactive**, which is what the sender-anonymity ruling already assumes for rings.

## 6.6 Filed rather than absorbed

The two-dials finding (§6.4) is **not** part of this candidate's build and must not be smuggled
into it. It wants its own ticket and its own decision from Nick.

## Additional sources

- [Meta Engineering — Scam Alert on WhatsApp with E2EE and verifiability (2026-08-12)](https://engineering.fb.com/2026/08/12/security/how-were-building-scam-alert-whatsapp/)
- [SoK: Content Moderation Schemes in End-to-End Encrypted Systems (arXiv:2208.11147)](https://arxiv.org/pdf/2208.11147)
- [SoK: Content Moderation for End-to-End Encryption (arXiv:2303.03979)](https://arxiv.org/pdf/2303.03979)
- [Asymmetric Message Franking (UMD)](https://www.cs.umd.edu/~imiers/pdf/frank.pdf)
- [LiveKit — End-to-end encryption](https://docs.livekit.io/home/client/tracks/encryption/)
- [LiveKit — Encryption overview](https://docs.livekit.io/transport/encryption/)
