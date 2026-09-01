# 🜂 Cast — the receipt, and the complaint nobody can forge

*Movement 4. **SCOPE STAMP: DESIGN-ONLY. IMPLEMENTATION UNPROVEN.** Nothing here has
been built, and a green design pass is not a green code pass (standing ruling from
`docs/crucible/11-expand-contract-migrations/TEMPER.md`).*

---

## 1. The problem, restated honestly after Heat

Nick asked for an auditable island, for **anyone**. The thesis I carried into this
forge was: *if every island can prove it behaves, there is nothing left to be
reputable about.*

**That thesis is refuted, twice, from opposite directions.**

- **Externally.** Every deployed transparency system kept an admission layer for its
  proof-sources. Chrome admits CT logs at its own discretion, with uptime SLAs and an
  operator-diversity rule ("as recognized by Chrome"); Signal's KT hardcodes three
  named auditors. CONIKS promised no auditor class; every production descendant hired
  one, because self-monitoring cannot supply cross-client agreement. And at **N=2 the
  gossip graph is a single edge** — split-view detection is theatre here.
- **Internally, and this is the more embarrassing one.** `docs/design/06-identity-and-trust.md`
  **already carries this thesis**, sourced by two adversarial research passes, and it
  already states the honest limit my slogan regressed:

  > *"'No central point' is not a shipped outcome … The achievable, honest claim is 'a
  > per-island auditable directory and no global trusted root'."*

  The forge did not need to discover that KT is the answer. It is written down. What
  the forge found is that **the thesis as Nick and I stated it last session was a
  regression of a more careful position this repo already held** — a live instance of
  the previous session's own crux, at the level of the thesis rather than a file path.

So the design question is not *"should we build a key-transparency log?"* It is:

> **Given that the incumbent answer is correct-but-unbuildable right now, what is the
> smallest thing that makes an island's behaviour checkable by anyone, needs no second
> observer, leaks nothing, and is independently useful even if the whole auditability
> story is abandoned?**

### Why the incumbent is unbuildable *right now* (not wrong — blocked)

Design 06 Decision 3's KT log is gated on things nobody has built, in both repos:

| Gate | State |
|---|---|
| Key rotation / revocation semantics | **absent both sides** — #3589 (app), #1972, #1865 (island). *A KT log with no revocation semantics logs an unrevocable key.* |
| Multi-device identity model | absent (#17) |
| Gossip transport for roots | **unbuilt and untrusted** — #1578 blocked; #3800: `peers_service` is *"TEST-GRADE, POISONING UNDEFENDED"*. "The existing mesh" is a premise, not a fact. |
| key→account binding island-side | **does not exist** — #3774: `record_signing_key` *records rather than enforces* |
| App willing to draw a ✓ from it | **recorded NO** — `aiko_chat_app/docs/crucible/key-continuity/DESIGN.md`: *"Recommendation: WAIT … Do not ship the interim as a security feature or a badge."* |

Shipping an island-side log now hands the app a mechanism it has already decided it
cannot draw a checkmark from. That is the definition of unpaid work.

---

## 2. The proposed shape

Two pieces, deliberately separable, in strict order. **Piece A is the conventional
core and is worth building on its own merits even if Piece B is never built.**

### Piece A (core) — the counterparty receipt

When the island accepts a client's signed message, it returns a **receipt**: a small
Ed25519-signed structure, over canonical length-prefixed bytes with its own domain tag,
binding together —

```
aikochat:receipt:v1:EdDSA
  island_id
  island_key_version
  channel_id
  client_msg_id     <- the frame id the client chose
  assigned_ulid     <- the position the island gave it
  accepted_at_ms
```

— and handed **only to the submitting client**. Not published. Not logged publicly. Not
gossiped. The receipt tells its holder exactly one thing they did not already know:
*the position this island committed to giving my message, signed.*

**Piece A closes a known, unowned island-side hole, independently of everything else in
this document.** The app tab measured it and nobody owns the fix:

> *"`message_view` carries no frame-level `client_msg_id`, so the check is
> self-referential … A dishonest gateway could relocate a validly-signed origin onto a
> different row with identical channel/body/reply and it verifies. … A truthful ✓ needs
> **both** fixed."* — `aiko_chat_app/docs/crucible/key-continuity/DESIGN.md`

Content-integrity is already solved by user signatures. **Position-binding is not**, and
the receipt is exactly the missing binding: the island signs *which row it put your
message in*, so relocating it later contradicts the island's own signature.

### Piece B (the atypical element) — the conformance prober

A small, publishable, runnable-by-anyone client that behaves like an ordinary user and
knows the protocol's correct responses by construction. It submits, collects receipts,
and checks them against what the protocol says must be true.

When an island returns a receipt that is provably wrong — a `client_msg_id` it did not
send, a `channel_id` it is not in, two different `assigned_ulid`s for one
`client_msg_id`, a receipt over content the prober never submitted — the prober holds
**the island's own signature over a false statement.**

That artifact is the thing Heat says survives:

| Heat's refutation of the incumbent | Bites here? |
|---|---|
| deployed systems grew an admission layer for proof-sources | **no** — the evidence is the accused island's own signature; there is no proof-source to admit |
| non-equivocation needs observer density, N=2 is one edge | **no** — one complainant suffices; N=1 |
| CT gossip died of a privacy deadlock | **no** — nothing is published or gossiped |
| the auditor class reappears | **no** — the complainant needs no standing; anyone can run the prober |
| comparative judgement re-imports reputation | **no** — a conviction is a yes/no about ONE island, never a ranking |

**And the by-product resolves in the honest direction.** There is no positive history to
accrue. An island's standing is *the absence of a conviction anyone chose to publish* —
uncheatable in the whitewashing sense (redeploying does not delete a signed receipt in
someone's pocket) and unfarmable (there is no score, only outstanding complaints). What
survives is not age-of-attested-history. It is **liability**.

---

## 3. Build order (core first, each step independently useful)

**Step 0 — not code, and probably the highest value per hour in this document.**
claude-tasks **#2161**'s remaining steps are *"(3) show Nick BEFORE posting; (4) post to
aiko_chat Discussions."* Andy raised the "softened CA" question that started this whole
thread on 2026-07-17. **He has never seen the answer**, which is written, sourced, and
sitting in Design 06. Publishing it costs one review pass and zero engineering, and it
is the only step here that involves the person whose question this was. It also does not
depend on any of what follows.

**Step 1 — echo `client_msg_id` on the message frame** (island). Additive wire field,
no schema change, no signing. Independently closes half of the position-binding gap and
is a prerequisite for anything else. Smallest possible increment.

**Step 2 — mint and return the receipt** (island). New domain tag
`aikochat:receipt:v1:EdDSA`, reusing `island_identity`'s existing Ed25519 key and its
established canonical-bytes discipline. Response-only: **no new storage**, so no
migration and no Alembic head risk.

**Step 3 — client retains receipts** (app). Store alongside the carried record. The app
already has the exact primitive — `carried_record.dart:96` re-verifies from carried
bytes alone with no network and no trust in a cached verdict; a receipt store is the
same pattern with a different subject key.

**Step 4 — the prober** (new, standalone, any language). Publishable, documented,
runnable by a stranger. This is the "anyone" in Nick's ask, made literal.

**Step 5 — decide what a conviction is FOR.** Deliberately last and deliberately
unspecified here (see §6). Steps 1–4 produce evidence; what anyone *does* with it is a
social question, not an engineering one, and designing the punishment before the
evidence exists is how reputation systems get built by accident.

---

## 4. Blast radius and consent spine

- **`MANIFEST_KEYS` is NOT touched.** It is an exact-set check at verify time, so any
  added key is a structural reject on every peer, and that `v` bump is already spoken
  for by #3731's media-posture split. The receipt is a **separate object with its own
  domain tag**, exactly as `island_identity` is separate from `signing`. Domain
  separation is already this codebase's established discipline and this design follows
  it rather than extending the manifest.
- **No schema change** through Step 3. No Alembic head, no `batch_alter_table`, no
  SQLite FK interaction (ISL-0001/ISL-0002 untouched).
- **Sender anonymity is preserved by construction, and this is the design's whole
  reason for rejecting Kelvin's public log.** A receipt is handed to the one party who
  was already there. Nothing is published, so there is no social graph to leak and no
  refused-ring record to keep — both decided rulings hold without a guard, because the
  coupling is removed rather than mitigated.
- **Trust boundary ⇒ cage-match by law** (project CLAUDE.md). Steps 1–2 touch the wire
  and a signing surface. This design is a *design*; the code gets a real cage-match.
- **The prober is a probe pointed at live islands.** Its safe target must be named
  before it is ever run: a prober firing at `chat.imagineering.cc` spends real users'
  island. Piece B needs a local multi-island harness — which is **already an open
  ticket, #2235** — before it points anywhere real.

---

## 5. Claims to falsify (the load-bearing assumptions, stated as things that could be wrong)

1. **C-1. A receipt is something an island can be made to sign.** An operator who
   simply *refuses* to issue receipts is not convicted of anything — they are
   indistinguishable from an island that is down. If refusal is free, the whole
   mechanism is opt-in for the honest. *This is the one I am least sure of.*
2. **C-2. The prober can tell "wrong" from "unlucky."** Heat's strongest empirical
   finding is that most real transparency-log failures were **operational, not
   adversarial** — botched restores, a reused test key, a dead operator, a cosmic-ray
   bit flip. A prober that reads a crashed island as a lying one produces mostly false
   accusations, which is worse than no prober.
3. **C-3. Position-binding is genuinely unowned.** Asserted from the app tab's document
   plus a tracker search that found no ticket. An absence-of-evidence claim; the
   coverage boundary is "the searches in RESEARCH-crosstab.md §5."
4. **C-4. A conviction is non-comparative in practice, not just in principle.** The
   moment two islands both have zero convictions, a user choosing between them is back
   to a judgement the design claims to have dissolved. Heat showed this exact creep
   happening in CT (binary per-SCT check, discretionary admission list).
5. **C-5. Response-only receipts need no storage.** If the island must later prove *it*
   issued a given receipt — or detect that it issued two — it needs state after all, and
   Step 2's "no migration" claim collapses.
6. **C-6. The app will accept this when it has declined the adjacent thing.**
   `key-continuity/DESIGN.md` says WAIT on trust claims. This design argues the receipt
   is *not* such a claim (it binds position, not identity) — but that is my reading of
   another tab's ruling, and the app tab owns it, not me.

## 6. Rejected alternatives

- **Build Design 06 Decision 3's KT log now.** Rejected on gates, not on merit — §1's
  table. It remains the right long-run answer and this design is explicitly *not* a
  replacement for it.
- **Kelvin's public receipt log.** Rejected: a published routing log is a published
  social graph, which collides with a decided ruling and re-walks the privacy deadlock
  that killed CT gossip. The log was never the load-bearing part.
- **Maxwell's expiring-delegation island.** Genuinely interesting and rejected as
  out-of-scope: it is a re-architecture of what an island *is*, not an increment, and
  it would invalidate the history and roster semantics the whole system rests on. Filed
  as a thought, not a plan.
- **Age-of-externally-attested-history as a first-class signal.** Rejected as the
  falsifier predicted: it requires comparing islands, and a comparison is a ranking, and
  a ranking is the reputation system #1569 already refuted. It dies correctly.
- **An admitted third-party auditor** (the WhatsApp/Signal shape). Rejected for now: it
  works, it is field-proven, and it reintroduces exactly the admission decision Nick's
  "anyone" was reaching past. Worth reopening if Piece B's conviction path fails.

## 7. Open variables (enumerated, not rounded to "ready")

- **V-1.** Does the receipt cover the message *body hash*, or only its position? Body
  coverage makes the receipt a stronger artifact and a bigger privacy object.
- **V-2.** What is the receipt's behaviour under the bus round-trip? The island is not
  the origin of ULIDs in every path; `assigned_ulid` may not be known at response time.
  **This is a factual question about the code that I have not verified**, and it could
  reorder Steps 1–2.
- **V-3.** Does the prober need an identity at all, or can it be fully anonymous? An
  identified prober is easier to discriminate against; an anonymous one is harder to
  rate-limit honestly.
- **V-4.** Steps 1–2 are island-side but Step 3 is app-side. Per the cross-tab rule this
  needs the app tab's agreement **before** merge, not after.
