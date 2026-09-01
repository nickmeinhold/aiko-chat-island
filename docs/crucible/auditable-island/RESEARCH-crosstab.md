# RESEARCH — cross-tab grounding pass (auditable-island)

**Scope:** what is ALREADY DECIDED, in the app repo (`../aiko_chat_app`) and this
repo, bearing on: per-island key-transparency (Merkle) log, signed island
manifests gossiped between islands, client-side proof verification, externally
attested history. **Not a design.** Conflicts are reported, not tie-broken.

Read date: 2026-09-02. Sources: `aiko_chat_app/docs/adr/` (all 5 present + index),
`aiko_chat_app/docs/crucible/` (7 dirs), `aiko-chat-island/docs/design/06`,
`nickmeinhold/claude-tasks` (10 global searches).

---

## HEADLINE — the topic is not new; it is a Draft decision of record in THIS repo

`docs/design/06-identity-and-trust.md` **already proposes exactly this thesis**,
backed by two adversarially-verified research passes. Its Decision 3 is the KT
log; its "What Changes" already names gossiped signed roots. Status line, verbatim:

> Status: Draft for discussion. Not a decided spec. Backed by two
> adversarially-verified research passes (formal theory + a survivor autopsy of
> shipped systems); load-bearing claims are cited inline. Spec deltas and a task
> breakdown are deferred until the direction is agreed.

So the crucible's job is **not** "should we do KT" — that argument is written and
sourced. It is: *the direction was never agreed, no spec delta exists, and the
client half has no foundation* (§4). Treat Design 06 as the incumbent design;
anything the crucible produces that contradicts it is a finding.

---

## 1. App-repo ADRs — full inventory

| ADR | Title | Status (from the doc) | Bears on topic |
|---|---|---|---|
| 0001 | The federation vocabulary | Accepted (retroactive; two named forks open) | YES — *IslandDirectory* fork is open |
| 0002 | EC for observation, API for structure | Accepted (retroactive) | Weakly — bans EC as a mutation/contract path |
| 0003 | Users: ChatServer CRUD + general-purpose ECConsumer | **Reserved, unwritten** (Andy + Nick) | no |
| 0004 | Sovereign identity federation — the identity anchor | **Draft, requesting comments** | YES — the governing constraint |
| 0005 | The identity graph | Draft, requesting comments | YES — Principal graph, Model B |
| 0006 | Sybil resistance: reputation, not personhood | Draft, requesting comments | YES — genesis question |
| 0007 | Porting Aiko Services to other languages | Reserved (Andy) | no |

**Note on status:** ADR-0004/0005/0006 are all **Draft**, not Accepted. The
project CLAUDE.md rule ("a numbered ADR *outcome* outranks an older handoff") is
weaker here than assumed — these are proposals of record, not ratified outcomes.
That is itself worth surfacing.

### ADR-0004 (the governing one) — load-bearing verbatim

Identity anchor:

> Identity is anchored to your **key**, not your home island. Concretely: **AIKO
> is email + cryptographic authorship.** … We build **no central directory** —
> cross-island discovery is a **federated per-island member roster** you browse
> island-first, gated on the federation handshake, with **opt-out** visibility.

The precision box that a KT design MUST respect (it strikes the compressed slogan):

> the **key is the unit of *authorship continuity*** … A person's **social
> identity is key + observed history + home-scoped label + contact caches +
> recovery policy** — the key does not, by itself, carry the handle, the
> reputation, or the recovery story. The temper struck down "the key alone
> certifies handle + reputation"; do not let the snappy slogan quietly re-import
> that error in reverse.

Blocking dependency on THIS repo, verbatim:

> This ADR specifies one half of a two-party contract. It has **zero shipped
> value** until `aiko-chat-island` agrees and implements the server side: the
> `IdentityDoc` shape, signature verification, and the roster endpoint with its
> opt-out enforcement.

The functional split (KT would be the mechanism under rows 3–5):

| Function | Locus (ADR-0004) |
|---|---|
| Authorship (who signed) | key, carried in the envelope |
| Identity continuity | key |
| Name allocation | home namespace (`nick:imagineering`) |
| Discovery | federated per-island roster, opt-out, peering-gated |
| Recovery / rotation | home-assisted re-attestation (v1) **+ a key-history chain from day 1 of any rotation** |
| Data / channel order + history | home ULID sequencer |

Increment 1 (the only thing sanctioned to build now):

> a **versioned `IdentityDoc`** — opaque/stable `id`, `verificationMethod[]`
> (signing keys, a *set*), `rotationKeys[]` (empty in v1), `alsoKnownAs[]` …
> optional `origin` as a **locator field only**. Doc versioning (monotonic seq or
> hash-linked `prev`) from day 1. **Origin is banned from primary/foreign keys,
> author IDs, ACL subjects, and mention targets** … No trust-root move, no wire
> break, no directory. This is the whole of what we build now.

Rejected, verbatim: **"Central directory in v1. Deferred, not built"**;
`did:key`/origin as durable primary key; passkey-as-identity-key.

Unresolved Q5 is the KT log's actual job description:

> **Current-ownership freshness.** A self-signed "I am @nick" proves a *claim*,
> not *current* assignment; the home signs the handle-assignment as a locator
> claim with freshness/revocation (Increment 2).

### ADR-0005 — one invariant a KT log must not break

> **One invariant.** Every stake, slash, bond, and rate limit is an operation on
> the Principal graph only. If a design needs to slash a Participant or trust a
> topic path, something upstream is wrong.

Open Q4 is the portability story a KT log would formalise: *"is 'port the key,
re-earn the standing, arrive with a letter-of-introduction vouch' the right
cross-island story?"*

### ADR-0006 — the genesis question, unanswered

> **Genesis:** what vouches for the *first* island operator, and how does a new
> island earn federation standing from zero? (Feeds ADR-0001's IslandDirectory
> fork.)

This is the exact hole an "auditable by construction" thesis claims to fill and
must be graded against. Also: **"Global structural detection: refuted by the
verified literature pass; local whitelisting only."**

### ADR-0001 — the open fork the topic reopens

> **IslandDirectory.** One pass names it (federated discovery of Islands, distinct
> from Registrar); the other folds it into future sibling services. Leaning
> toward naming it: two-level discovery … is a real seam with different trust
> properties on each side.

---

## 2. App-repo designs, crucibles, handoffs

Crucible dirs present: `chatskin`, `federated-identity-anchor` (→ ADR-0004),
`group-e2ee-key-management`, `island-operator-seat`, `key-continuity`,
`pop-identity-binding`, `sovereign-message-signing` (+ `wire-half`).

**`key-continuity/DESIGN.md` is the single most load-bearing app-side document for
this topic** — it is the app tab's recorded reasoning about exactly the trust
question a KT log answers, and its verdict is WAIT. Verbatim:

> **Status: DESIGN ONLY. Nothing here ships.** … the naive version is
> unshippable.

> The ✓ is held not because the app can't verify a signature (it can …), but
> because it takes the **key → account binding on the island's word**. A
> malicious/compromised island can mint a key, TOFU-register it as any user,
> sign, and the app would render ✓.

> **A naive pin-and-warn is unshippable**: it false-positives on every legit key
> change (second device, recovery re-key, rotation). Distinguishing legit
> rotation from attack is **genuinely gated** on the multi-device identity model
> (#17) and rotation semantics (#21 / island #1865). Continuity cannot ship
> before them.

> **Recommendation: WAIT for real continuity (gated on #17/#21). Do not ship the
> interim as a security feature or a badge.**

And a second, independent defect that a KT log does **not** fix:

> **Content-integrity, not position-binding** … `message_view` carries no
> frame-level `client_msg_id`, so the check is self-referential …
> A dishonest gateway could relocate a validly-signed origin onto a different row
> with identical channel/body/reply and it verifies.
> **Continuity addresses reason 1. It does NOT address reason 2** … A truthful ✓
> needs *both* fixed.

Position-binding is an **island** change (carry and echo the frame
`client_msg_id`) and is currently owned by nobody.

**Handoff files:** the only `HANDOFF-*` artifacts on disk are in THIS repo —
`HANDOFF-to-app-tab-v2-social-wire.md` and `docs/handoff-app-auth.md`. Neither
touches KT/transparency. **No app-tab handoff is superseded by an ADR on this
topic** — the supersession warning in CLAUDE.md does not fire here.

---

## 3. Island `docs/design/06-identity-and-trust.md` — ALL decisions

Read end to end (265 lines). Five numbered Decisions:

- **Decision 1 — The key is the principal (SPKI/SDSI).** Keep what exists; PoP is
  the whole authentication story. *"No third party vouches for a binding that is
  definitional, not asserted."* Consistent with ADR-0004.
- **Decision 2 — Petnames for memorability, not for trust.** Constraint: *"do not
  make human verification the trust defense"* — ~1/3 blind-accept a MITM'd safety
  number (Turner 2023); 21 of 28 sophisticated users failed a live MITM (Schröder
  2016). Implementation note: trustwords + traffic light, **never raw fingerprint
  comparison**. **This directly constrains any KT client UX.**
- **Decision 3 — A per-island auditable key-transparency log.** The topic.
  *"each binding carries a cryptographic proof of consistency against an
  append-only Merkle log. Each user monitors only their own binding … Provider
  equivocation … becomes cryptographically self-incriminating; cross-island
  gossip of signed log roots is what makes a split view detectable."* Carries a
  named production lesson: **Apple's Signed Mutation Timestamps** — *"Plan
  promise-then-merge, not epoch-gated availability."*
- **Decision 4 — The wallet pattern confirms the recovery design we already
  have.** *"no messaging system has shipped social-graph recovery at scale."*
  Argent = ceil(N/2)-of-N + mandatory 48h time-locked veto; Design 05 already has
  both halves. Not a change; external validation.
- **Decision 5 — Andy's "softened CA" is SPKI delegation, anchored in a log.**
  *"A per-island KT log **is** 'a CA you never have to fully trust.'"*

**Constraints/contradictions a KT build must carry:**

1. **The honest-claim reframe is mandatory, not optional.** *"'No central point'
   is not a shipped outcome … The achievable, honest claim is 'a per-island
   auditable directory and no global trusted root'."* Any crucible output
   claiming "trust by checking, not by trusting the operator" **overclaims** — KT
   makes equivocation *detectable*, it does not remove the trusted party.
2. **Introduction-of-strangers still does not scale.** *"Transitive social trust
   caps at about two hops in practice … No surveyed survivor escaped the two-hop
   ceiling without a central directory/log."* So "a stranger can trust an island
   by CHECKING it" is only true for *consistency*, never for *first contact* —
   the TOFU/bootstrap moment is untouched by a Merkle log.
3. **Design 06 §"Affected specs" already claims scope over `03-auth-on-the-bus`,
   `04-passkey-first-identity`, `05-social-recovery` and app Design 09** — a
   larger blast radius than a fresh crucible would assume.
4. **Its own open question 4 is unanswered and is a design fork, not a detail:**
   *"Does the KT log run per island, or is there a shared federated log with
   per-island namespaces?"* Open questions 1–3 are also live (no at-scale social
   recovery precedent outside wallets; Nostr/Bluesky key-loss rate unmeasured;
   what split-views CT gossip has actually caught).

**Note:** Design 06 lists *"a new key-transparency-log capability"* under
"Affected code (island)" and *"key/identity verification UX (petnames + optional
confirmation ceremony)"* under "Affected code (app)". The app half is **not
filed** as an app-repo task anywhere found (§5).

---

## 4. THE CLIENT-SIDE FEASIBILITY QUESTION — measured, with citations

**Verdict: the crypto primitives exist and are production-grade; the KT client
would be from near-zero, and the one place the client meets the island's signed
manifest today deliberately does NOT verify it.**

### What exists (real, shipped, cited)

- `pubspec.yaml`: `cryptography: ^2.9.0`, `flutter_secure_storage: ^10.3.1`.
- **Ed25519 sign + verify, production code:**
  `lib/features/chat/domain/message_signing.dart:112` `final Ed25519 _ed25519 = Ed25519();`
  — `sign()` at `:118`, `verifySignature()` at `:158` (`_ed25519.verify` at `:164`).
- **A canonical, length-prefixed, domain-separated byte encoder** —
  `signingBytes()` at `message_signing.dart:71`, domain tag
  `'aikochat:msg:v1:EdDSA'` at `:27`, pinned by golden vectors
  (`docs/crucible/sovereign-message-signing/SIGNING-SPEC.md`). A KT client's
  hash-input discipline could reuse this pattern directly.
- **A hardened untrusted-input admission gate**:
  `lib/features/chat/domain/origin_envelope.dart` — `validateOrigin`, base58btc
  Multikey decode (`_kB58Alphabet:71`), per-field caps (`:61-68`), charset gate
  before base64url decode (`:79`), frozen exact key-set (`:43`). Explicitly a
  *"byte-for-byte mirror of the gateway carrier's `validate_origin`"* (`:17`).
- **Secure storage of a long-lived Ed25519 identity key:**
  `lib/services/sovereign_key_store.dart` (seed at `:46`, single-flight mint at
  `:58`, derive-never-store-pubkey at `:86-94`).
- **A production verifier that is genuinely island-independent:**
  `lib/features/chat/domain/carried_record.dart:96` re-verifies each cached
  message *"from the carried bytes alone, with no network and no trust in the
  cached ingest-time verdict"* (`:1-4`), and — crucially — it already models the
  three-way verdict a KT client needs: `verified` / `invalid` / `foreignKey`
  (`:28-45`), binding `verified` to a **known subject public key**, not to the
  island's `sender.userId`.

### What does NOT exist

- **`verifySignature` has exactly ONE production caller** —
  `carried_record.dart:145`, reachable only from the Carried Record screen
  (`lib/app/router.dart:30` → `carried_record_screen.dart`). The normal message
  path does not verify inbound signatures. Every other call site is a test.
- **The island manifest is fetched and deliberately NOT verified.**
  `lib/features/settings/application/island_manifest_provider.dart` fetches
  `GET /v1/island` (`:58`) and keeps only `island_pubkey` (`:69`) to colour an
  avatar. Verbatim (`:26-31`):

  > NOT VERIFIED, and deliberately: the manifest is signed, but this only paints
  > a colour and a coastline. Checking a signature here would imply the mark is a
  > security claim — it is a recognition aid, and treating it as more than that
  > is how a decoration ends up load-bearing. If islands ever need to be
  > cryptographically identified in the UI, that is a different feature with its
  > own trust root, **and it should not inherit this one's cache.**

  It also swallows every failure (`:75-79`) and TOFU-pins the pubkey in
  SharedPreferences keyed by host (`:32`, `:74`) — **a plaintext, unauthenticated
  pin that a KT design must not mistake for an existing trust anchor.**
- **No Merkle/inclusion/consistency-proof code anywhere.** No hash-tree, no
  `sha256` tree walk, no log-root storage, no monitor loop.
- **No key-state / rotation surface.** claude-tasks **#3589** measured it:
  `keyState` in `lib/`: **0 hits**; `keyVersion`: 25 (positive control). So the
  design's own "additive later" hook is unfunded.
- **No island-key allowlist / peer-trust store** on either side (island #3800).

### The consequence for feasibility

A KT client needs three things. One exists, two do not:

| Need | State |
|---|---|
| Ed25519 verify over canonical bytes | **EXISTS**, hardened, golden-vector-pinned |
| Merkle inclusion + consistency proof verification | **ZERO** — from scratch |
| A monitor that watches *my own* binding over time, plus somewhere to pin roots | **ZERO** — and the only existing pin (`island_manifest_provider`) is explicitly disclaimed as non-security |

And the app tab's own recorded position (`key-continuity/DESIGN.md`) is that the
client-side trust work is **gated on the multi-device model (#17) and rotation
semantics (#21 / island #1865)** — none of which are built.

---

## 5. Tracker prior art (global search, all labels, all states)

Searched: `transparency`, `key transparency`, `CONIKS`, `merkle`, `audit`,
`auditable`, `reputation`, `gossip`, `equivocation`, `attestation`.

**Direct hits — the topic already has a ticket:**

| # | State | Title | What it settles / asks |
|---|---|---|---|
| **2161** | OPEN | Author island identity/trust design note (CA-free 3-layer stack) for aiko_chat discussions | **This IS the topic's ticket.** Origin: Andy's 2026-07-17 "softened CA" proposal. Verdict recorded in the body; Design 06 is its partial output. **Its unfinished steps are: post to aiko_chat Discussions** — *"(3) show Nick BEFORE posting; (4) post to aiko_chat Discussions."* Backed by research runs `wqyx5r483` (25/25 3-0) and `wddjwu189`. |
| **3796** | OPEN | The manifest's honesty is carried by a boot guard that Phase B lifts | **Load-bearing, and it corrects itself.** The KT binding *"is **not an undiscovered prerequisite** — it is a named Phase B deliverable"* in `docs/crucible/09-operator-mode-election/BLADE.md`: *"device/member transparency (binds to note 06 KT log)"*. The real finding: **that Phase B list is FLAT and unordered**, so MLS could ship before member transparency. Restated acceptance: *"member/device transparency must land no later than the commit that makes `e2ee` bootable."* And it reshapes the threat: *"the property needing evidence is not 'does the operator hold plaintext' but **'is the group I am encrypting to the group I believe it is'** — equivocation about MEMBERSHIP, not about posture."* |
| **3800** | OPEN | `peers_service`'s trust banner points at a CLOSED issue | The gossip layer this topic would ride is **TEST-GRADE, POISONING UNDEFENDED**; its deferral pointer (#1546) is closed. Also verified same-day: *"`island_identity.verify_manifest` proves a manifest was signed by the key IN the manifest — it does NOT prove that key belongs to the island you meant to reach."* Quotes **Nick, 2026-09-01: "auditability must work for ANYONE, by construction — which makes peer authenticity a product property, not an operator practice."** Names Design 06 Decision 3 as the designed mechanism. |
| **3774** | OPEN (`aiko_chat_app`) | An evidence viewer MUST verify signatures itself and never render the island's attribution | App-side invariant. **The gap KT would close, stated precisely:** *"the island does NOT bind `origin.sender_pubkey` to the authenticated account. `signing_keys` has `UniqueConstraint("user_id","pubkey")` — per-user, not globally exclusive — and `record_signing_key` is a first-seen upsert that RECORDS rather than ENFORCES."* Second-order limit for UI copy: *"the log proves 'THIS KEY sent these messages', not 'THIS PERSON did'."* |
| **1569** | OPEN | Design 09: sybil-resistance via per-island economic cost, not personhood | The genesis/cold-start half. Rich comment thread (bonded vouching, conserved vouching, root-anchored EigenTrust, grey-market analysis). **Its own prior-art flag:** *"do NOT build assuming this synthesis is novel"* (SybilGuard/SybilLimit, Advogato, EigenTrust, Friedman-Resnick, TrustDavis). |
| 1582 | OPEN | Design 09 adoption (#1569): adopt sybil-resistance HTML into gateway repo | Doc-placement chore, still open. |
| 1578 | OPEN | Gateway directory (PR#52): fix cage-match blockers then wire imagineering↔enspyr gossip | **The gossip transport this topic assumes is not built** — PR#52 blocked on SSRF/OOM/rate-limit. |
| 2507 | OPEN | A5 prerequisite: clear-on-failed-admit policy for sticky peer mode | Federation-handshake adjacency. |
| 2235 | OPEN | Local multi-island federation test harness | The instrument any gossip/equivocation test would need. |
| 3589 | OPEN (app) | App-side signing-key rotation/revocation lifecycle — the `keyState` hook DESIGN.md claims but never built | *"a stolen seed signs valid messages as the user **forever** … Reinstalling the app does not help — it mints a NEW key."* **A KT log with no revocation semantics logs an unrevocable key.** |
| 3590 | OPEN (app) | Sovereign key inherits a bare `FlutterSecureStorage` default — backup-restorable identity seed | **Direct contradiction on the record** (see §6). |
| 1972 | OPEN (app) | PoP key lifecycle: DELETE/revocation + compromise/rotation semantics (Temper gap) | Island-side twin of 3589. |
| 1962 | OPEN (app) | E2EE keystone: signed-not-sealed | The MLS side #3796 binds to. *"signing only makes a relay trustless for INTEGRITY … an untrusted island can READ EVERYTHING."* |
| 1941 | OPEN (app) | Federation: identity = your public key (unify the two identities the app already holds) | Predates and is largely absorbed by ADR-0004. |
| 2506 | OPEN | Promote takedown event to a signed, subject+reason-bearing carried judgment | The "externally attested history" by-product has a nearby existing ticket. |
| 1865 | (ref'd) | Island `signing_keys` registry: soft-revoke tombstone, key_version lifecycle, pubkey collision index | Named by 3589 as the island half. |
| 2405 | OPEN | Dreaming-citizens H3 — sovereignty crypto (⚠️ run /crucible before build) | Adjacent, cross-labelled to this repo. |

**Nothing was found for "externally attested history" / "age of history" as a
concept** — that by-product appears genuinely un-filed.

---

## 6. CONFLICTS AND FINDINGS (surfaced, not resolved)

**C1 — "no central directory" (ADR-0004) vs. a KT log as trust anchor.** Not a
contradiction on its face — Design 06 anticipates it (*"Prefer per-island
sovereignty … over one shared directory"*) — **but Design 06's own open question
4 leaves the fork open**, and a *shared federated log with per-island namespaces*
would be a directory in all but name, which ADR-0004 explicitly rejects for v1:
*"Central directory in v1. Deferred, not built."* The crucible must pick the
per-island arm or reopen an ADR-level decision. **Do not let a "log" smuggle in a
"directory".**

**C2 — the thesis overclaims against Design 06's own honesty clause.** The topic
says *"a stranger can trust an island by CHECKING it rather than by trusting its
operator."* Design 06: *"'No central point' is not a shipped outcome … Every
surviving KT and recoverable-identity deployment … reintroduced a central
directory plus log operator and made it auditable rather than eliminating it."*
KT buys **detectable equivocation over time**, not trustlessness at first
contact. The honest framing is already written; the thesis as stated regresses it.

**C3 — ADR-0004 vs. the shipped app storage default (open, unresolved, #3590).**
ADR-0004: recovery is home-assisted re-attestation, *"explicitly not identity-key
recovery"*; a passkey copy is *"a login continuity factor, never recovery."*
Measured code: `sovereign_key_store.dart:61` passes no platform options, so the
seed inherits `KeychainAccessibility.unlocked` — **not** `_this_device` — and is
therefore restorable onto a different handset. #3590: *"an encrypted backup
restore IS identity-key recovery, happening silently, outside that model, with no
attestation and no key-history entry."* **A KT log that assumes one key ⇒ one
device would be logging a premise the code violates today.**

**C4 — the KT log's first job (key→account binding) is exactly what the island
does NOT do, and the app tab has recorded a WAIT on shipping any trust claim
built on it.** #3774 (island is a carrier, `record_signing_key` records rather
than enforces) + `key-continuity/DESIGN.md` (*"Recommendation: WAIT … Do not ship
the interim as a security feature or a badge"*). A KT crucible that produces an
island-side log without moving #17/#21/#1865 hands the app a mechanism it has
already decided it cannot draw a ✓ from.

**C5 — position-binding is a second, orthogonal hole a KT log does not touch,
and it is an ISLAND fix owned by nobody.** `key-continuity/DESIGN.md`: *"A
dishonest gateway could relocate a validly-signed origin onto a different row …
and it verifies. … A truthful ✓ needs *both* fixed."* The island change (carry +
echo the frame `client_msg_id`) has no ticket found in the searches above.

**C6 — ordering, per #3796.** *"member/device transparency must land no later
than the commit that makes `e2ee` bootable — or a recorded decision prices
shipping without it."* This is the strongest existing argument for doing the work
NOW, and it is already on the record; the crucible should adopt it rather than
re-derive it.

**C7 — the transport the design assumes is unbuilt and untrusted.** Design 06
plans *"cross-island gossip of signed log roots over the existing mesh"*, but
#1578 says the gateway directory / gossip wiring is blocked on cage-match
blockers, and #3800 says `peers_service`'s trust model is *"TEST-GRADE, POISONING
UNDEFENDED"*. **"The existing mesh" is a premise, not a fact.**

**C8 — status inflation risk.** ADR-0004/0005/0006 are **Draft, requesting
comments**, and island Design 06 is **"Draft for discussion. Not a decided
spec."** Nothing on this topic is Accepted anywhere. #2161's own remaining steps
(*show Nick, then post to aiko_chat Discussions*) were never done — so Andy, who
raised the softened-CA question that started this, **has never seen the answer**.
That is arguably the cheapest high-value action available and it predates any
build.

---

## 7. What a KT crucible may safely assume, and may not

**May assume (recorded, sourced):**
- key-as-principal (SPKI/SDSI), PoP as the whole authn story (Design 06 D1 /
  ADR-0004).
- petnames are usability, never the trust gate; no raw-fingerprint UX (D2).
- promise-then-merge (Apple SMT), not epoch-gated availability (D3).
- guardian-quorum recovery already matches the wallet pattern (D4); ceil(N/2) +
  48h veto.
- Ed25519 verification over canonical length-prefixed bytes is real, hardened and
  golden-vector-pinned on the client.

**May NOT assume:**
- that "no central point" is achievable or claimable (D3 risks section).
- that a gossip mesh exists to carry roots (#1578, #3800).
- that key→account binding exists island-side (#3774).
- that keys can be rotated or revoked on either side (#3589, #1972, #1865).
- that one key means one device (#3590).
- that the client verifies anything the island sends today, including the island
  manifest it already fetches (`island_manifest_provider.dart:26`).
- that ADRs on this topic are settled — all are Draft.
