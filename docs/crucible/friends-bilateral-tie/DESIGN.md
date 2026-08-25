# 🜂 DESIGN — the consented bilateral tie

> Movement 4 (Cast). 2026-08-25. Status: **cast, un-folded, un-tempered.** Not build-ready.
> Bundle: `CRUCIBLE.md` (ore + falsifier), `RESEARCH.md` (heat, incl. §6 E2EE addendum),
> `SPARK.md` (four sparks + fusion). Task #3420. Design PR homes to `geekscape/aiko_chat`.

---

## 0. Blast radius and consent spine — up front, per the forge's own rule

**The dangerous thing this design introduces is a single unauthenticated mutating endpoint**
(`POST /v1/ring`). Today every mutating route on the island requires a session. This one cannot,
by construction — a session names the caller, and naming the caller is the thing we are removing.

| | |
|---|---|
| **Owner** | Island (Nick) for the endpoint + token mint; app tab for the pairing ceremony, grading UX and consent surfaces |
| **Injection surface** | `POST /v1/ring` — unauthenticated, accepts an opaque sealed blob and a token |
| **Throttle** | (a) token issuance is rate-limited per authenticated account; (b) the **existing** `APNS_WAKE_PER_RECIPIENT_PER_MINUTE=6` bounds flooding of any one recipient; (c) hard payload size cap |
| **Fail direction** | CLOSED — an unverifiable token, an unknown recipient, or an oversized payload is refused with a constant-shape error before any DB write |
| **Consent** | Nothing is ever added to anyone. A tie requires a deliberate act on **both** devices; a grade is set by the **recipient**; melting a bell is unilateral and instant |
| **Blast if wrong** | An attacker who forges tokens can wake phones (battery + notification noise), bounded by the per-recipient budget. They **cannot** read ties, learn the graph, or make a phone ring — a wake it cannot decrypt is silent |

**No DB write happens before token verification.** Token check → recipient resolution → budget →
push. That ordering is load-bearing and is the first thing a reviewer should attack.

---

## 1. Problem

The island has no representation of *two people who both agreed*. Memberships, blocks and mutes
are all unilateral. Two consequences:

1. **Reach has no consent gate.** `push_service` can wake a sleeping handset and has no concept
   of whether the person being woken ever agreed to be reachable by the caller. Its only proxy is
   "you two share a 1:1 DM", which is co-location, not consent.
2. **Nobody is ever asked.** `invite_only` is a `JoinPolicy` value meaning *admin-add-only*
   (`models.py:50`). There is no pending-invite row and no invitee consent step.

And it must be solved without the island becoming a queryable social graph — ADR-0004's *no
central directory*, app Design 05's *the graph never crosses to the client*, and island Design
05's **C5**, whose security argument depends on an island-compromising attacker being unable to
learn who would warn you.

---

## 2. The shape

### 2.1 The one idea that makes it tractable

> **An edge needs both ends. Hide either one and the edge is hidden.**

This is the design's load-bearing simplification and it arrived late (see §6, C-1). Spark's fused
object hid the **recipient** (broadcast into the fog) and paid a broadcast bill. But the friend
graph is a set of *pairs* — so hiding the **sender** conceals the pair just as completely, and
hiding the sender is *cheap*: one ordinary addressed push, no broadcast, no cohort, no
intersection attack to defend against.

So: **the ring is addressed to the recipient and anonymous in the sender.** The island learns
"someone rang B at 19:04". It never learns who, and therefore never learns an edge.

This is Signal's **sealed sender**, and the bilateral tie is the *unidentified-access credential*
that Signal's version needs. Consent and anonymity become one object: **you can only be anonymous
to people who agreed to hear from you.**

### 2.2 What lives where

| | island | device |
|---|---|---|
| the tie (`K_ab`, label, icon) | **nothing** | both endpoints |
| the grade ("how loudly may B reach me") | **nothing** | the **recipient's** device |
| block | **nothing** (moves off-island) | the recipient's device |
| ban | account status (unchanged) | — |
| delivery address | user id → device tokens (**already exists**) | — |
| wake tokens | blind-signature keypair + spent-token set | unspent tokens |

**The island's half of "friends" is zero rows.** That is the atypical element, and it is the
whole point: there is no table to leak, subpoena, or misconfigure.

### 2.3 The tie ("a bell", after Carnot's spark)

Created by a two-sided ceremony (QR, deep link, or an in-band offer/accept pair). Yields on each
device a shared tie key `K_ab`, a label, and — set independently by each side for the *inbound*
direction — a **grade**:

| grade | behaviour on a closed handset |
|---|---|
| `urgent` | rings through Do Not Disturb |
| `ring` | rings normally |
| `glow` | notification only, no sound |
| `silent` | delivered, never surfaced |

Grading is not decoration; it is the reason a tie is richer than a boolean. Reachability becomes
*how loudly may this person reach me*, owned entirely by the person being reached, enforced on
their own hardware where it cannot be coerced or leaked.

### 2.4 The ring flow

```
A: seal = AEAD(K_ab, {caller_label, call_id, room_hint, sent_at})
A: spend one blind-signed token T
A: POST /v1/ring  {recipient_id, T, seal}          # NO session
island: verify T is our signature, unspent, unexpired   -> else 4xx, no write
island: resolve recipient_id -> device tokens           -> else 4xx
island: per-recipient wake budget                       -> else drop
island: mark T spent; push {seal} to B's devices
B:  try AEAD-open with each held tie key
      fail  -> discard, SILENT, no notification, no trace
      ok    -> apply B's stored grade for A -> urgent/ring/glow/silent
```

The island holds a sealed blob it cannot open, addressed to a person, paid for by a token it
cannot attribute.

### 2.5 The new gate map — because gate 0 does not apply here

`push_service`'s gate 0 is that a wake rides only downstream of an accepted `create_outbound`,
which is why block/ban/idempotency traverse for free. Its docstring names the exit condition
exactly: *"A future caller that wakes a device WITHOUT an accepted message behind it… needs its
own gate map."* This is that caller. The replacement, gate for gate:

| old (via gate 0) | new |
|---|---|
| sender authenticated | **valid unspent island-minted token** |
| banned sender refused at ingress | **ban bites at token issuance** — no session, no tokens; short token TTL bounds the latency |
| blocked pair refused by `create_outbound` | **block moves to the recipient's device** — a melted bell cannot be opened |
| resend idempotent on `client_msg_id` | **tokens are single-use** — a replayed ring is a double-spend |
| DM/private/one-peer channel checks | **not applicable** — a ring names a person, not a channel |
| per-recipient wake budget | **unchanged, reused** |
| — | **payload size cap** (new; an unauthenticated endpoint has no other backstop) |

**Block moving to the device is a genuine change of posture, not a loss.** A block the island
enforces can be leaked, mis-set, or coerced; a block your own phone enforces cannot.

### 2.6 Recovery — the tie is bilateral, so your friends hold your half

Device-only ties die with devices, and social recovery exists *for* losing your device — so
without this the recovery path hands you back an empty world (§6, C-3).

Resolved **not** by escrowing anything. Design 05 v2 deliberately eliminated every reconstructable
secret (its finding C3), and finalize *"revokes old passkeys and old signing keys"*. There is
nothing left to decrypt a stash with, by design.

Instead: **a tie is bilateral, so losing your phone destroys only your half.** After recovery:

1. The island publishes a **re-key digest**: `GET /v1/rekeyed?since=` — a flat list of account
   ids that re-keyed recently. Every client fetches the *same* list.
2. Each device **intersects locally** against its own tie list. No per-friend query is ever made,
   so the island never learns whose recovery you care about.
3. A match raises a deliberate prompt on the friend's device — *"Nick recovered their account.
   Re-establish?"* — requiring explicit confirmation, mirroring Design 05's guardian-approval UX.
4. On confirmation, the friend re-offers their half; the tie is rebuilt.

Spark's *broadcast the rare thing* survives here — applied to recovery events, where the list is
tiny and the leak is "who recovered", not "who knows whom".

**Honest degradation:** friends who never come back online are never recovered. That is also true
of a real social circle.

### 2.7 Invite-accept falls out

The same primitive: an offer that is inert until the other side completes it. A room hands you a
silent half-tie; pressing back completes it and the room may then reach you. **Invitation stops
being admin insertion and becomes something you were asked.**

---

## 3. Build order — core first, each step independently useful

| # | increment | island work | ships on its own? |
|---|---|---|---|
| **1** | **The tie + consent ceremony.** Pairing, `K_ab`, labels. Rings ride the **existing** message path (island sees the sender, exactly as today — no anonymity claim yet). | ~none | ✅ consented ties exist; nobody is added without asking |
| **2** | **Graded reachability.** Recipient-set grades enforced on-device against the existing wake. | none | ✅ *"emergency bell rings through sleep, kitchen bell only glows"* — real user value, zero crypto |
| **3** | **Invite-accept.** A pending, consented invitation built on the tie. | small | ✅ closes the *nobody is ever asked* gap |
| **4** | **Sender-anonymity.** Blind-signature mint, `POST /v1/ring`, the §2.5 gate map, block-moves-to-device. | **large** | ✅ the island stops learning who calls whom |
| **5** | **Recovery re-pairing.** Re-key digest + local intersection + deliberate re-offer. | small | ✅ closes the data-loss interaction with Design 05 |

**Increments 1–3 need essentially no island change and no cryptography**, and deliver the product
value Nick asked for. Increment 4 is where the cost and the risk live, and it is deliberately
*last* — so the feature is useful long before the expensive property is bought, and so increment
4 can be abandoned without losing anything already shipped.

---

## 4. Tradeoffs, named with owners

| tradeoff | accepted cost | mitigation / owner |
|---|---|---|
| **IP address defeats sender-anonymity** | The claim is *"the island cannot LINK a ring to an account in its own data"*, **not** "the island doesn't know who rang". Without a mixnet this is an operator promise about logging | Named residual. Don't log / truncate at ingress. **Owner: Nick.** Any stronger phrasing in code, docs or manifest is an overclaim |
| **Ban latency = token TTL** | A banned account keeps unspent tokens until they expire | Short TTL (24h proposed — an open variable). Owner: island |
| **Block leaves the island** | The island can no longer refuse a blocked ring | The recipient's device silently discards. Arguably stronger. Owner: app tab |
| **Anonymous flooding wakes phones** | Battery/notification noise, never a ring | Existing per-recipient budget + issuance limits |
| **Blind-signature machinery at 46 users** | Real crypto in Dart and Python for a small population | Increment 4 is last and abandonable. **This is the objection I most expect at Temper** |
| **Media E2EE is orthogonal** | Not part of this build | Filed as **#3426**, must not be smuggled in |

---

## 5. Rejected alternatives

- **A server-side friend table.** Simplest possible thing. Rejected: it *is* the queryable social
  graph, and it hands an island-compromising attacker exactly the list C5 needs them not to have.
- **Hashed / PPID pairs.** Rejected on measurement, not taste: the island holds both the key and
  the roster, so it brute-forces all N² pairs — **1,089 hashes at 33 users.** Small N makes this
  weaker, not stronger.
- **Full-island broadcast (Tesla's fog).** Beautiful, and unnecessary once §2.1 is seen — hiding
  the sender already hides the edge. Also likely infeasible on iOS: silent `content-available`
  pushes are throttled, and PushKit pushes **must** be reported to CallKit, so a broadcast would
  raise a spurious call UI on every handset on the island.
- **Cohort narrowcast (k-anonymity).** Rejected: cohorts are defeated by intersection over
  repeated observation, and a *fixed* cohort is just a smaller graph.
- **Per-tie delivery "mouths".** Rejected: the count of mouths resolving to a person's devices
  **leaks their degree** — the same defect as Kelvin's per-account token piles.
- **Capability token with no tie.** Rejected: no revocation story, no grading, and nothing for the
  recipient to own.
- **App-local contact list, no island change.** The true null option. Rejected because it cannot
  ring — reach is precisely the thing that requires the island to act.
- **Escrowed friend bundle keyed to recovery.** Rejected on a *recorded* strike: Design 05's C3
  killed reconstructable secrets. There is nothing left to encrypt to.

---

## 6. Claims to falsify — the enthusiasm's load-bearing assumptions

- **C-1 — "hiding one end hides the edge."** The whole simplification. If the island can
  reconstruct the sender by other means (timing against WS presence, IP, token-issuance
  correlation, per-recipient volume fingerprints), the design's central property is void and the
  broadcast bill comes back.
- **C-2 — "an unauthenticated mutating endpoint can be made safe with a token."** This is the
  single largest new attack surface on the island. Ordering (verify before write) is asserted, not
  yet proven.
- **C-3 — "friends re-hand you your half" is an acceptable recovery story.** Untested against a
  real social graph's liveness. If most ties are with people who rarely open the app, recovery
  silently fails for the majority.
- **C-4 — "the re-key digest doesn't leak the graph."** Everyone fetching the same list is the
  argument. Does a client's *fetch cadence*, or the timing of a subsequent re-offer, leak the
  intersection back?
- **C-5 — "a sealed ring can reach a closed handset at all."** **UNPROVEN IN PRODUCTION.**
  `device_tokens` = **0 on both islands** (#3253); `APNS_USE_SANDBOX=true` on both, so only debug
  builds could ever ring (#3386). This design's primary consumer has never once worked end to end.
- **C-6 — "blind signatures are worth their weight at 46 users."** The plain alternative
  (rate-limit per authenticated sender) is ten lines. Increment 4 buys sender-anonymity and
  nothing else. If sender-anonymity is not worth a large crypto build, increments 1–3 stand alone
  and increment 4 should be cut.
- **C-7 — "block moving to the device is not a regression."** Asserted as *stronger*. A cold
  reader may reasonably call it a moderation capability the island silently gave up.

---

## 7. Open variables — enumerated, not rounded to "ready"

1. **Principal-level or per-island tie?** (ADR-0005's invariant vs ADR-0004's no-directory.) **Nick's.**
2. **Does a tie survive the island dying?** Design 05's open question 2 asks the same of the recovery policy. **Nick's.**
3. **Does banning an agent implicate its voucher?** (ADR-0005 Model B.) **Nick's** (#3421).
4. Token TTL — the ban-latency dial. Proposed 24h.
5. Blind-signature scheme — RSA blind signatures vs VOPRF/Privacy Pass. Undecided; driven by what is credible in Dart.
6. Does a tie need to be *provable to a third party* (a room admitting a guest on a friend's word), or is it purely two-party? Currently assumed two-party.
7. Cross-island ring routing: which island mints the token when A and B are on different boxes?

---

# 8. FOLD — the author's own adversarial pass (2026-08-25, pre-Temper)

> Movement 5. No round budget — this is just me, and it is cheap. Findings are folded back into
> the design above where they change it, and recorded here where they change the *claims*.
> Fold works the metal; it does not re-grade the ore.

## 8.1 Degenerate states

**F-1 — Bootstrap is undefined, and discovery is forbidden.** §2.3 hand-waves *"QR, deep link, or
an in-band offer/accept pair"*. But an in-band offer must address a user id, and ADR-0004 forbids
a central directory — so where does the id come from? **Answer, and it must be stated rather than
assumed:** the only directory-shaped surface that exists is the **shared-channel roster**, and it
is deliberately scoped (`members.py`: *"a public channel's roster is enumerable by any member who
can read it — public channels are public by definition"*). So the bootstrap is **"you may offer a
tie to someone you share a channel with, or to someone out-of-band via QR/link"** — and nothing
else. This is a *narrowing* of the design, and it should be written into §2.3 rather than left
open, because the obvious "friend search" affordance an app designer would reach for is exactly
what ADR-0004 prohibits.

**F-2 — Simultaneous offers create two ties.** A offers B while B offers A. Two half-ties, two
keys; both decrypt (a device tries all held keys), but the *grades* diverge — A's grade for B sits
on one tie and B's on the other. **Fix, folded in:** derive the tie key deterministically and
symmetrically, `K_ab = KDF(ECDH(pk_A, pk_B) ‖ domain_tag)`, so simultaneous offers converge on the
identical key and the ceremony is idempotent. This also removes any need for tie ids to be
negotiated.

**F-3 — Multi-device is entirely unaddressed, and it is also the cheapest backup.** A tie made on
A's phone is invisible to A's laptop, which can neither ring nor be rung. Two consequences: (a)
the design owes a device-to-device tie-sync story; (b) **a second device is itself a recovery
mechanism** — strictly better than C-3's friends-re-hand-you-your-half, because it needs nobody
else's liveness. §2.6 should present multi-device as the *first* line of recovery and social
re-pairing as the fallback, not the only path.

**F-4 — Double-spend is asserted, not designed.** §2.4 says *"mark T spent"*. Two concurrent POSTs
with the same token is a classic TOCTOU, and this codebase already has the answer twice
(`consume_challenge`, `_capped_insert`, and Design 05's C2 fix): **one guarded statement with the
condition folded into the WHERE, arbitrated by rowcount, failing closed on `rowcount != 1`.**
Folded into §2.4 as a requirement, not an implementation note.

**F-5 — The spent-token set grows without bound.** Every ring writes a permanent replay-guard row.
Prunable only because tokens expire: once a token is past TTL it cannot be spent regardless, so
its spend record can be dropped. Needs an explicit retention rule tied to the TTL (open
variable 4).

**F-6 — Unauthenticated endpoint = account-enumeration oracle.** This is the worst of the
degenerate cases. `POST /v1/ring` is unauthenticated, so response differences between *recipient
exists*, *recipient exists but has no device* (**the current state of every user on both
islands**), and *recipient does not exist* let an anonymous caller enumerate the user base — and
map who has a handset registered. **Fix, folded in:** constant-shape, constant-status response for
every one of those cases after token validation, matching Design 05's *"constant-shape errors"*
convention. Spend the token either way, so timing and side effects do not distinguish.

**F-7 — Ciphertext length leaks the caller.** The seal contains `caller_label`. Labels have
different lengths, so an island watching sealed blobs can bucket senders by size and, over time,
fingerprint them. Sender-anonymity leaking through a length field would be embarrassing and it is
easy to close: **pad the seal to a fixed size.** Folded in.

**F-8 — The re-key digest is public.** `GET /v1/rekeyed?since=` reveals *who recently lost their
device or was compromised*, to anyone. The local-intersection property does not require the list
be public — only that everyone fetch the **same** list. **Fix:** require a session. Folded in.

**F-9 — Ringing a deleted account.** A is deleted; B still holds the tie. A can no longer mint
tokens (no session), so nothing rings and the tie simply goes quiet. Acceptable, but B never
learns A is gone. Named, not fixed — the alternative is telling B about A's deletion, which is an
island-side statement about a relationship the island is not supposed to know.

## 8.2 Stressing the claims

**C-1 is WEAKER THAN CAST CLAIMED, in two ways — both mine to own.**

1. **Token minting is authenticated, and correlates.** The island sees *A minted tokens at 19:04*
   and *a ring arrived at B at 19:04*. If tokens are fetched on demand just before a ring, the
   correlation is near-perfect and the whole property collapses. **Fix, folded in: mint in
   batches, well in advance, on a schedule decoupled from use** (e.g. a daily top-up at an hour
   the client picks). This is a *requirement*, not an optimisation.
2. **The anonymity set is the concurrently-plausible sender population, and at N=33 that can be
   one.** The island sees WebSocket presence. A ring landing at 03:00 when exactly one account is
   online identifies the sender without any cryptography being broken. **This does not have a
   fix at this scale** — it is a property of the population, not the protocol.

   **Consequence, stated plainly: increment 4's value scales with population, and at 46 users it
   buys much less than it costs.** That is not a reason to redesign it; it is a reason to keep it
   last and to be honest that it may never be worth building. It also sharpens **C-6** from "is
   the crypto worth its weight" to "the property being purchased is itself weak at current
   scale."

**C-3 is weakened by F-3 and should be demoted.** Multi-device is a better recovery story and
needs no one else's liveness. Social re-pairing becomes the fallback for the genuinely
single-device user.

**C-5 is unchanged and remains the one that matters.** No amount of folding substitutes for a row
in `device_tokens`.

**C-7 (block moving to the device) survived the fold**, with one caveat worth handing the
adversary: a device-enforced block cannot stop the *wake*, only the ring. A determined harasser
can still cost a blocked recipient battery and a silent notification, bounded by the per-recipient
budget. The island genuinely gives up the ability to stop that, and calling this purely "stronger"
is an overclaim — it is **stronger against coercion and leakage, weaker against nuisance.**

## 8.3 Trying to dissolve the problem with the simplest rejected alternative

The null option in §5 was *"app-local contact list, no island change — rejected because it cannot
ring."* Folding honestly: **that rejection was wrong, and it is the most important finding here.**

Increments 1–3 route rings over the *existing* message path, which already reaches a handset
through `create_outbound` → `push_service`. So increments 1–3 **are** the null option — an
app-local contact list with grades — and they work. The island change is nil.

Which means:

> **The genuinely novel island-side content of this entire design is increment 4 — and §8.2 just
> established that increment 4 buys a weak property at current scale.**

Two honest readings, and I am not going to pick between them for Nick:

- **The elegant reading.** The right island-side answer to "friends" is *almost nothing*, and this
  forge's real output is knowing precisely which nothing, and which one thing the island must
  never do (hold the pair). A design that ends in "don't build it here" is a result.
- **The deflationary reading.** This candidate is mostly an app feature wearing an island
  crucible's clothes, and the island half should be closed with a short ADR rather than a build.

Either way **increments 1–3 remain worth shipping and are unaffected** — the product value Nick
asked for (consented ties, graded reachability, being *asked* rather than added) survives
completely and needs no island work.

## 8.4 What Fold changed

Folded into the design above: F-2 (deterministic symmetric key), F-4 (guarded single-statement
spend), F-6 (constant-shape responses), F-7 (fixed-size padding), F-8 (authenticated digest),
plus C-1's batched-minting requirement and F-1's explicit bootstrap narrowing.

Recorded as weakened claims rather than fixes: C-1 (small anonymity set — unfixable at this
scale), C-3 (demoted below multi-device), C-7 (nuisance caveat).

**New open variable 8:** does increment 4 ever get built, given §8.3? That is Nick's call and it
should be made *before* the crypto work is scoped, not after.
