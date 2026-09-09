# Design 12a — the island's answer to the app tab's CallKit temper findings

**Answers:** claude-tasks#3744 (four findings the island half owns), filed by the app tab
2026-09-01 from its 4/4 `/design-temper` on app design 16.
**Amends:** design 12 (`12-native-call-ui-callkit-connectionservice.md`) — Decisions 1c, 4, 5.
**Status:** the island's position, under `/design-temper` as of 2026-09-09. Not a decision of
record until it is struck, **except** finding 1's ceiling inversion, which Nick ruled on
2026-09-09 and which is marked in place.

**Nick's rulings folded here:** finding 1 (the island takes the ring ceiling) — DECIDED.
Finding 4 (Recents) — still open, question restated on the correct premise and awaiting him.

---

## The headline: a three-way contradiction, stated by neither tab

> **CORRECTION 2026-09-10 — "fleet-wide revocation" is WRONG, and it was mine.**
> Apple's `PKPushRegistryDelegate` documentation, fetched verbatim after Nick asked one word
> — *"penalises? Or denies?"*:
> *"On iOS 13.0 and later, if you fail to report a call to CallKit, the system will terminate
> your app. Repeatedly failing to report calls **may** cause the system to stop delivering any
> more VoIP push notifications to your app."*
> Three errors in what this document carried: it **DENIES delivery, it does not revoke a
> privilege** (nothing is taken away — the OS stops handing pushes over); it is **PER-DEVICE,
> not fleet-wide** (*"the system"* is the OS on that handset, corroborated by
> `CSDVoIPApplicationKillCounts` living in the device-local `com.apple.TelephonyUtilities`
> domain — evidence we held for hours without connecting); and *"may cause"* means it is **not
> deterministic**, so "unrecoverable" was unearned too.
> **The conclusions in this document do not change, and one gets STRONGER.** A single failure
> terminating the app IS deterministic. And per-device denial is *harder* to detect than a
> fleet-wide event: it accumulates silently on the handsets taking the most calls, so calling
> quietly stops working for your heaviest users with nothing surfacing anywhere.
> **Provenance, which is the real finding.** The phrase entered as Tesla's temper wording and
> was restated four times across two repos, each restatement reading as established fact. Four
> adversarial families did not catch it **because it was never written as a claim** — it arrived
> as background colour inside an argument about something else. The raw strike files in
> `temper-strikes-12a/` are deliberately NOT edited: a temper records what was said at a moment,
> and this error was in the restating.



The four findings are real and I accept three of them outright. But reading them against our
own design 12 surfaces something bigger that sits underneath all four, and that neither
document contains:

1. **The app tab's recast spine** (design 16 arm iii, landed on independently by Carnot and
   Kelvin) is *ring-eligibility is a property of an established relationship*.
2. **That eligibility fact is device-local and publishes nothing.** `RingAllowlistStore` has
   shipped since Nick's 2026-08-26 ruling; its own docstring rejects an island-held roster,
   on ADR-0004's refusal of a central directory.
3. **Design 12 Decision 4 requires the ring/no-ring fork to happen at the island's send
   door**, because *"there is no on-device window in which to reconsider"* — a VoIP push must
   be reported to CallKit before the handler returns.

**So the decision must be made where the fact is not.** All three are individually correct and
individually well-argued. Any two are compatible. All three are not.

This is the actual joint question. It is upstream of the poll-versus-push argument that has
been standing in for it (island #4023), and upstream of whether PR #167 merges.

### RESOLVED — the trilemma dissolves, because outcome 3 is not binary

The app tab supplied the solvent within the hour, and it is the better answer. **Claim 3 is
only contradictory if ring/no-ring is a binary.** It is not. There are three outcomes:

| outcome | who determines it |
|---|---|
| **silent** — no push leaves the island | island only |
| **momentary ring** — VoIP sent, Swift verifies, fails, ends immediately | device only |
| **sustained ring** — VoIP sent, Swift verifies, passes | device only |

Device-local consent fully determines *momentary vs sustained*. **That is the on-device
window design 12 Decision 4 says does not exist** — bounded to the report-then-end interval,
but real. Only *silent vs momentary* needs a decision where the fact is not.

So claims 1 + 2 + 3-as-stated are contradictory; **1 + 2 + 3′ are consistent**, where 3′ is
the narrower true claim: **only the island can produce silence.** Design 16 v2 §0 already
contains the distinction — it downgrades the property to *"no **sustained** ring before
proof"* — without noting that it answers a trilemma nobody had framed.

**Which changes the question.** It is not "which two do we keep." It is **what is a momentary
ring worth, and to whom?** Two costs, different owners:

- **To the user** — a quarter-second buzz through silent mode and DND from someone they
  refused. Bounded, but it is precisely the harassment surface, and it is *observable by the
  attacker*.
- **To us** — flaw 9. A poor report-and-end ratio may cause iOS to stop delivering VoIP pushes **to that handset** (see the correction above). That is
  the real ceiling, and it is why the verify set has to be *right* rather than merely fast.

Design 12 Decision 4's sentence — *"there is no on-device window in which to reconsider"* —
is therefore **too strong as written** and should be narrowed to "no window in which to
*silence*". Both documents' errors here are the same error: treating an interval as a point.

The arms below are kept because the costing still stands, but they are now arms on the
**narrowed** question, not the trilemma.

### The three arms

**(A) Publish the consent fact.** `should_wake` gains a consent input, the fork becomes
three-way (voip / alert / neither), and the island can decide correctly at the door.
**Cost:** it puts "may this person wake me at 3am" under an operator's control, which is the
exact thing the app tab's store docstring refuses and which ADR-0004 refuses generally. I do
not recommend this and I do not think it is Nick's likely call, but it must be on the list
because it is the only arm that needs no new machinery.

**(B) Ring first, verify in Swift, end immediately on failure.** The app tab's own
post-strike addendum already establishes this arm is available: Swift is alive in
`pushRegistry(_:didReceiveIncomingPushWith:)`, CryptoKit Ed25519 is microseconds, and the
consented key set is small, local and known before the push arrives. The island always sends
VoIP for an invite; the device decides whether it survives.
**Cost, stated honestly:** a non-consented caller produces a **sub-second ring flash**, not
silence. That is a real product cost and it is Nick's to price, not mine. It also collides
head-on with finding 4 (Recents) — see below, because it is the same cost wearing a different
coat.

**(C) A ring capability the island can check but not read.** The callee's device issues a
blind-signed wake token at consent time; an invite carries it; the island verifies the
signature and learns only *"this invite carries a valid ring capability"* — never who
consented to whom, never who is calling.

**I recommended (C) here. That recommendation is withdrawn, and the way I reached it is worth
recording, because it is the exact failure this document catches other people making.**

I wrote that (C) *"is not a new idea I am importing — it is the direction already ruled in."*
Nick's 2026-08-25 ruling is about **anonymity**: the island should not learn who is calling.
It is **not** an endorsement of capabilities as the **ring gate**. Those are different axes,
and claude-tasks#3745 says so in terms: *"This decision makes the ring SAFE without making it
ANONYMOUS — the app tab had bundled those and they are orthogonal."* I un-bundled nothing; I
re-bundled them, and upgraded a ruling on one axis into authority over another. The app tab
caught it, having had the same move caught on it by Nick forty minutes earlier.

**And that same #3745 comment cuts harder against (C) than either of us argued:**

> **restricting the caller set SHRINKS the anonymity set.** Friends/consent makes the island's
> picture of who-rang-whom *more* precise, not less. Friends decides who *may* ring; blind
> tokens would decide whether the operator *learns who did*.

So consent and anonymity do not merely fail to imply one another — on this path they pull
in **opposite** directions. A mechanism serving both was never going to be free.

**Three objections to (C), hardest first:**

**(a) Revocation asymmetry — disqualifying in this shape.** Device-local consent revokes *at
the enforcement point*, instantly, by the person being woken. A minted capability sits **in
the caller's hands** and cannot be revoked, only expired. For a mechanism whose entire purpose
is that the sleeper controls who may wake them, that inverts who holds the off switch.
Verified, not hypothetical: **claude-tasks#3521 is open and confirmed pre-existing on `main`**
— revoking consent mid-ring makes the hangup unadmittable, because `admitCallEnd` re-reads
current consent and a refused end *deliberately keeps ringing*. Capabilities make that class
strictly worse. The staleness worry and this are one defect from two ends: short expiry is the
only fix, short expiry needs frequent re-minting, and re-minting needs a live channel — the
one thing a locked handset does not have.

**(b) The ratchet above.**

**(c) Distribution.** A capability must reach the caller over some channel, and that channel
is a new trust surface with its own story. Device-local consent needs no distribution at all,
which is its main virtue rather than an incidental one.

**Corrected position: (B) is the spine — arms 1 + 2 + 3′ above — with the momentary ring
priced honestly. (C) is held as the named escape if flaw 9 measurement says the
report-and-end ratio is untenable**, because it buys the one cell nothing else does: silence
for a refused caller. Neither document should harden around it yet.

### Pushing back on objection (a), since it was invited — it narrows rather than falls

The app tab asked to be pushed on revocation, as the load-bearing objection. Honest answer:
**it is partly answerable, the answer costs something specific, and the part it does not
answer is the part #3521 actually names.**

*Partly answerable.* Revocation of a bearer capability is a solved shape: the island holds a
set of revoked capability identifiers and refuses them at the send door. The callee publishes
"this id is dead" without revealing whose it was or who held it, so the island still learns
neither side of the tie. The off switch returns to the sleeper.

*What it costs.* If a capability carries a stable id so it can be revoked, the island can
**link rings that reuse it** — it learns "the same relationship rang N times", which is not
who, but is more than nothing. One-time tokens remove the linkage and remove revocability with
it, because you cannot revoke a token that has not been minted. So the arm is really
**unlinkable / revocable / offline-mintable — pick two**, and a locked handset with no live
channel is what forces the third. That is a sharper statement of the same tension the app tab
identified, not a refutation of it.

*What it does not answer at all.* **Mid-ring revocation — #3521's actual case — survives every
construction above.** A revocation list is consulted at send time; a ring already reported to
CallKit is already in flight, and no island-side state can retract it. Device-local consent
revokes at the enforcement point, which is the only place that helps once the phone is
already ringing.

**So the objection is sustained, on a narrower and better-stated reason:** not "capabilities
cannot be revoked" (they can, at a linkability price), but **"no island-side mechanism can
revoke a ring that is already ringing, and that is the case the open bug is about."**

Confidence, marked: the revocation-list construction is standard and I am confident it exists;
I am **not** confident about its unlinkability properties under the specific blind-signature
scheme this would use, and that is a question for someone who does this for a living rather
than for either tab. Nothing above should be built on without that check.

**Two guardrails carried on (C) for whenever it is picked up, because this claim has inflated
once already** — twice now, counting the axis-upgrade above. Its ruled scope is
sender-side: *"the island cannot link a ring to an account in its own data."* It does **not**
buy recipient anonymity — the island must hold the device token to send the wake, so it knows
who is being woken, and that survives at N=33 because the binding constraint is APNs, not
scale. And it is void at the IP layer without a mixnet: that remains an operator promise
about logging, not a property of the system. Anything stronger is an overclaim.

---

## Finding 1 — Decision 1c may be assigning the app tab something it cannot hold

> **DECIDED — Nick, 2026-09-09: "yep". The island takes the ceiling.**
> Design 12 Decision 1c's assignment to the app tab is **reversed and dead**; do not build
> against it. The mechanism of record is the **ring lease expiry** below — the island expires
> its own push, it does not infer an end, so Decision 1's boundary survives. The wake carrying
> it must be distinguishable at the client from a signed client end.
> The three-clock question below is **not** settled by this ruling and stays open.
>
> The reasoning that led here is kept unedited, including the part arguing it should not be
> decided by two tabs agreeing — that is why it was put to Nick rather than resolved here.

**Both tabs agree the island should take the ceiling. That is not enough to move it, and it
is not recorded as settled here.**

The app tab's engineering position and mine converged independently, and it flagged the
right hazard in doing so: *"two Claude tabs agreeing is the exact failure mode we should both
be watching for — a wrong answer wearing two signatures survives review better than either
error alone, because the second signature reads as verification."* Correct, and it applies
with extra force here because this **reverses a recorded decision in design 12 that names the
app tab as owner**. Surfacing it and then resolving it by mutual concurrence is still
tie-breaking. So it goes to Nick with both positions, below.

The technical case is nonetheless strong, and it is stronger than the app tab put it, because
**design 12 already contradicts itself on this point and neither half cites the other.**

Decision 1c says the app tab owns re-establishing the 30s ceiling, and files the consequence
as *"flagged, not acted on."* Seventy lines later, Decision 4 says:

> The client cannot receive a VoIP push, check liveness, and decline to ring — it must ring
> first. **So send-time correctness moves onto the island.**

Decision 4 had already moved correctness to the island. Decision 1c assigned the ceiling to
the client anyway. Tesla did not find a new fact; Tesla found that our document states the
fact in one section and the opposite assignment in another. Two families reasoning from the
same doc missed it, which is what a premise gets instead of scrutiny.

The app tab is also right that it *"had no home"* — no app-side design note and no task. We
recorded a commitment as taken that had never been written down anywhere. That is a
process finding worth keeping separately from the technical one.

### But taking it back breaks Decision 1

Decision 1 is explicit: **the island never *infers* an end. It carries and wakes on one.**
A ceiling is an inferred end. So "the island owns the ceiling" and "the island never infers
an end" cannot both stand as written.

**Proposed resolution — reframe from *call end* to *ring lease expiry*.** The island is not
asserting the caller hung up; it is expiring **its own push**. It already owns a lifetime
here: `apns.py:_EXPIRATION_SECONDS = 60`, with the reason written beside it (*"a ring that
surfaces late is worse than no ring"*). A lease expiry is a fact about the island's own
delivery, not a claim about the call — so Decision 1's boundary survives intact, and the wake
that carries it must be **distinguishable at the client** from a signed client end, never
forged to look like one.

**Nick — the decision, with both positions.** Design 12 Decision 1c records the app tab as
owning the 30s ring ceiling. Both tabs now think that assignment is wrong and that the island
should own it, because a CallKit ring does not self-expire and the app is suspendable the
instant the report completes — so a Dart timer is a fiction and a Swift timer is not
obviously better. **The case against moving it:** it grows the island's role in a call from
carrier to enforcer, which is the direction the #3170 DISSOLVE pushed back on, and it means
the island emits a wake nobody's client asked for. Neither tab is neutral here — we both
built the thing that would change. It is your call.

### While we are here: there are three clocks and nobody owns their relationship

| Clock | Value | Owner |
|---|---|---|
| APNs push expiration | 60s | island, `apns.py` |
| Ring duration | 30s | app, `kCallRingDuration` |
| Ring freshness window | 10s | app |

Three constants, three homes, no stated relationship, and the ceiling question above depends
on all three. The lease and the ring should be derived from one another rather than picked
independently — the app tab already did exactly this inside its own repo when it bound
`CallEndAnnouncer._ackWait` to `kCallRingDuration` and wrote *"a bound derived from the thing
it is bounding, rather than picked."* Same move, one layer up, across the repo boundary.

---

## Finding 2 — Decision 6's opacity and on-device proof cannot both be true

**BOUNDED, not dissolved — and the island owes nothing for the part that is bounded.**

I first wrote "dissolved" here; the app tab corrected it and the correction is right, so the
weaker word stands with the two residuals named.

The opacity half genuinely resolves. Tesla's *"opacity and on-device proof cannot both be
true"* was true only while verification had to read the payload. Once ring-eligibility is a
property of an established relationship, Swift verifies against a small set of already-allowed
local keys and **the payload never names anyone**. Design 12 Decision 6 stands unchanged: the
payload stays `c`-only, and the deleted identity-resolution endpoint stays deleted.

**What does not resolve, and must not close silently:**

- **Key-set freshness at wake time.** Swift reads an App Group cache that Dart maintains. A
  caller consented to five minutes ago, on a phone that has not foregrounded since, is not in
  it — so a *legitimate* ring fails proof. Design 16's own open questions leave this to the
  recast; it is not answered anywhere yet.
- **The property is "no *sustained* ring before proof", not "no ring before proof."** Every
  VoIP push must be reported to CallKit before the handler returns, so the arm is
  *verify → report → immediately end on failure*, never *verify-or-silence*. A forged push
  still produces a momentary ring. Stating the stronger version would be an overclaim, and
  the residual lands in flaw 9, where a poor report-and-end ratio costs VoIP delivery
  to that handset.

The second of those is the same sub-second flash that arm (B) and finding 4 both pay for —
now visible in a third place, which is the clearest argument yet that they are one cost and
should be priced once.

Recording it here rather than letting it sit resolved-only-in-the-app-tab's-file, because
finding 2 was raised as a cross-repo conflict and a cross-repo conflict needs its closure
written on both sides.

**Note the dependency, though:** this dissolution is *conditional on the recast spine*. It
resolves under arms (B) and (C) above. Under arm (A) it stays dissolved too, but for a
different reason. It is only a live conflict if the eligibility question is answered in a way
that puts caller identity back in the payload — which no arm currently does.

---

## Finding 3 — Decision 5's end-sentinel wake needs a client dispatch contract

**ACCEPTED, and the app tab is right that it may need a different transport. The defect is on
our side: Decision 4's predicate is misnamed.**

Decision 4 forks the send path on *"is this a call?"*. An end sentinel **is** call-related, so
that predicate routes it to VoIP, and a VoIP delivery must be reported to CallKit before the
handler returns — which turns the stop into a start. The app tab named the consequence; the
cause is that we picked the wrong question.

**The fork is not call / not-call. It is ring-starting / not-ring-starting.**

```
starts a ring?   -> voip token  + <bundle>.voip topic + push-type voip
otherwise        -> alert token + <bundle> topic      + push-type alert
```

Under the corrected predicate an invite is VoIP, an end is not, and the app tab's request for
*"ends on a channel that does not carry the `reportNewIncomingCall` duty"* is satisfied by an
island-side change with no new transport invented.

**DO NOT harden Decision 4 around this yet.** The app tab's correction, which I accept: the
predicate fix is the **second** node of a tree whose first node is unmeasured, and half the
time it will be moot.

```
Does reportCall(with:endedAt:) ALONE satisfy iOS's must-report rule?
├─ YES → ends ride VoIP. Guaranteed wake, no second ring.
│        The predicate fix is UNNECESSARY; Decision 5's transport stands as written.
└─ NO  → ends must go alert. The predicate fix is NECESSARY, and
         "does an alert wake a locked, RINGING handset in time?"
         becomes load-bearing and needs its own measurement.
```

**The top node needs one handset, no island, and no gateway change** — send a local VoIP
push, report only `endedAt`, observe whether iOS complains or degrades delivery. That is the
discriminator and it is cheap. The island's two-handset alert-wake protocol tests the NO
branch specifically, so it should run *after*, not instead.

Both tabs arrived at this experiment independently (it is design 16 v2 §9 step 2), which is
worth less than it feels — we share a premise set, and that is exactly the condition under
which independent agreement is not evidence.

### The UUID contract — the island's half, pinned

The app tab is right that `payload UUID ↔ signed ULID ↔ end-key` is unwritten in both
documents. The island's half, per Decision 1 (the call id is client-minted and the island
carries it):

- **The payload UUID IS the client-minted call ULID.** Not a second identifier, not
  island-minted, not derived. One id, carried.
- **Therefore payload ULID ≡ signed ULID by construction**, and `reportCall(with:endedAt:)`
  can always find its ringing call.
- **The island never reuses a call id and never mints one.** It has no call object to mint
  from — that is Decision 1b, explicitly not this work.

If the app tab needs a CallKit-shaped UUID rather than a ULID, that is a client-side
derivation from the carried id, and it must be a pure function so both ends compute the same
one. Say so and I will write the island's guarantee to match.

---

## Finding 4 — Decision 6a's Recents ruling rests on a changed premise

**AGREED, and surfaced to Nick rather than tie-broken.** The ruling is design 12's, so it is
ours to re-put, not the app tab's to reverse.

Nick settled this 2026-08-30 on the reasoning that a Recents entry *"names a call they were
party to."* Under CallKit every `admitRing` refusal is post-hoc, so a blocked, muted or
unverified caller writes a Recents entry for a call the user was **not** party to. Tesla's
line is the one to put in front of him: **"Harassment fills Recents as 'Mom.'"**

**Nick — the question, restated on the correct premise:** you ruled that appearing in the
system Recents list is fine because the entry names a call you were party to. That premise
does not hold under CallKit: a blocked or forged caller can write an entry for a call your
own client would have silenced, and the entry can carry a cached roster name. Do you still
want `includesCallsInRecents = true` globally, or admit-gated per call?

**This is the same cost as arm (B) above**, which is why the two should be decided together
rather than separately: a sub-second ring flash from a non-consented caller and a spurious
Recents entry from one are the same event seen from two places. Arm (C) removes both at the
source, which is a second reason to prefer it.

**Attribution, carried rather than absorbed.** The **harassment-evidence reframe is
Deanna's** (2026-09-01, her first week as TPO). The **ring/record 2x2 — that ringing and
recording are independent axes, and "no ring + record" is the cell the bundled flag hides —
is the app tab's**, and had not appeared in any repo artifact before this one. Recorded here
so the ideas keep their owners as they cross repos.

**Not a jurisdictional fact:** the app tab has flagged that "the ring/record cells are
Deanna's to spec" was an inference a prior instance wrote into a memory note and then
restated until it read like a ruling. Her TPO role is real and Nick-sourced; that assignment
is not. Neither #3781 nor #3782 has an assignee. Nobody should lean on it as a decision.

---

## Finding 5 (ours) — #3744's own Context section rests on a premise its author retracted

Not a criticism, a drift note, and the mirror of the one we owe them.

#3744's closing Context section says arm (iii) *"publishes a per-conversation consent fact to
the island"* and that therefore `should_wake` gains a consent input. The app tab's **own
post-strike addendum, dated the same day**, retracts exactly that: `RingAllowlistStore` is
device-local by design and publishes nothing. *"The privacy cost that made (iii) a product
question was priced against a mechanism nobody was going to build."*

The consequence matters, because it is what produces the three-way contradiction at the top
of this document: `should_wake` gains **no** consent input, so the island's fork does **not**
become three-way on that axis, and the island is left needing a fact it is not allowed to
have. The retraction did not merely remove a cost — it moved the problem.

**And the symmetric correction we owe:** island #4023 cites *"app design 16 flaw 3
(end-sentinel transport)"* as the joint blocker on PR #167. That mischaracterises flaw 3,
which is the ring-ceiling ownership finding answered above. The transport worry is flaw 7.
#4023's wording is being fixed.

---

## What this means for PR #167 and the occupancy endpoint

**An earlier draft of this document recommended closing PR #167. That recommendation is
withdrawn, and the way it was reached is worth recording.**

It rested on a relayed status — "the app tab reckons video is done" — compressed on its way
to me. The app tab's actual words were **"built, live-proven, and shipped OFF"**: calling is
behind `kCallingEnabled = bool.fromEnvironment('ENABLE_CALLING')` and `dart_defines/prod.json`
carries no such key, so it is false in every shipped build including 0.0.4 on both stores.
"Live-proven but shipped off" and "done" are different facts, and I was one step from
retiring a capability and six live defects on the difference. Neither tab should act on the
other's status second-hand again — the record or nothing.

### The endpoint is not subsumed by the end sentinel

#3198 answers *"did the caller signal that they hung up?"*. #3159 answers *"is anyone
actually in the room?"*. Three cases where the first is silent and only the second speaks —
each verified against the app's source rather than accepted:

- **No end is ever sent.** Phone dies, app killed, network drops. Design 16 §2 names it:
  *"a caller whose phone dies sends no end. Liveness then rests entirely on the window, which
  is why the window cannot be large."*
- **The end is delivered and still cannot be applied.** `ring_controller.dart:78` holds ends
  in a plain in-memory Dart map inside a Riverpod controller — empty by construction on a
  push-woken cold start. That is temper flaw 6, and the file's **own docstring** says push
  makes it the normal case rather than the exotic one: *"the island wakes a handset on the
  INVITE body only, so a cold start processes the invitation first by construction and the
  end is an ordinary afterthought."* `_forget` also bounds the map to
  `kCallInviteFreshness * 2`, so even a warm process holds an end for seconds.
- **Answering into an empty room.** The caller drops without hanging up. There is no end to
  send and nothing to deliver; occupancy is the only instrument that reads that state.

So **#3198 is a best-effort happy-path signal and #3159 is the authoritative one.** They
share no coverage on the three cases above. Closing #3159 on the strength of #3198 would
retire a capability on the strength of a weaker instrument — which is what "the ring lease
replaces it" quietly assumed.

There is also a concrete cost in shipped client code: `ring_overlay.dart:96-100` chose the
word "Ignore" over "Decline" *because* no signal reaches the caller until #3159 lands. Close
it unbuilt and that comment becomes false and the word loses its justification.

**Corrected position: PR #167 stays open and the six MUST-FIX defects in #4023 part A get
fixed.** The ring-lease wake from finding 1 is an *addition* to occupancy, not a replacement
for it — it bounds a ring the client cannot bound, while occupancy answers a question no end
sentinel can. If the endpoint is ever struck it should be struck on its own merits.

---

## Owed, and not answered here

- **Measure it, do not reason about it — in this order, because the first may moot the
  second.** (1) Does `reportCall(with:endedAt:)` alone satisfy iOS's must-report rule? One
  handset, no island, app tab drives. (2) Only if (1) is NO: does an alert push wake a locked,
  *ringing* handset in time? Two handsets and a live island, island drives. Both documents are
  currently reasoning about an Apple behaviour neither tab has observed.
- **#3745 stays a separate thread — do not fold it into this answer.** I had it filed as
  "two incompatible ring designs, neither citing the other"; the app tab corrected the
  characterisation and I take it. Its core is the **sender-anonymity contradiction**: designs
  12 and 16 build the ring as a stored, attributable message, while Nick's 2026-08-25 ruling
  says the island should learn neither who is friends with whom nor who is calling. Folding
  it into a resolved findings answer would bury an independent open question inside one.
  Sharpened further on that thread: design 12's answer to the liveness finding is *"cancellation
  is a signed client message"* — its fix for one problem is **more** attributability, which
  runs *against* the anonymity ruling rather than merely being silent on it.
- **Design 16 v2 has LANDED** — `aiko_chat_app` PR #196, `docs/design/16-callkit-ring-v2.md`
  at **`8ee5ded`** (pinned deliberately, not the branch head), RECAST OF RECORD. It is cited
  here rather than predicted:
  - **§0** states the bounded property (*"no **sustained** ring before proof"*) and the flaw-9
    consequence. This document's headline section defers to it.
  - **§1c** holds the key-set-freshness crack and three candidate answers, recommending
    write-through on consent with multi-device as the stated residual. The island owes nothing
    for it; recorded here so it is not re-derived.
  - **§2** surfaces the Decision 1c inversion as SURFACED, NOT DECIDED — matching this
    document's position exactly, which is agreement between two tabs and therefore not a
    decision. It is Nick's.
- **Arm (C)'s actual cost** — blind-signed wake tokens are the ruled direction but have never
  been costed as an implementation. Before anyone treats (C) as chosen, that number exists
  and nobody has it.
- **The hard gate stands, and it binds us too.** From Carnot, adopted by the app tab: no
  `CXProviderDelegate` and no VoIP send path until the eligibility question is decided.
  Reversibility is lost once the phone rings.

## Provenance

Written 2026-09-09 from the primary sources, not from summaries: claude-tasks#3744 in full,
`aiko_chat_app/docs/design/16-callkit-ring-TEMPER.md` including its post-strike addendum,
`aiko_chat_app/lib/features/call/` as shipped, this repo's design 12, `apns.py`, and the
2026-08-25 sender-anonymity ruling **including its 2026-09-01 correction**, which is what
keeps arm (C)'s claim at sender-side scope instead of the recipient-anonymity overclaim a
previous reading imported from a spark.

Findings 1-4 are the app tab's, answered. Finding 5 and the three-way contradiction are the
island's, raised here for the first time.

**Revised a second time after the app tab's reply**, which dissolved this document's headline
trilemma (outcome 3 is not binary; only the island can produce *silence*), disqualified the
blind-capability arm on revocation asymmetry against the verified claude-tasks#3521, caught
the ratchet in my own recommendation, and showed the predicate fix to be the second node of a
tree whose first node is one cheap unmeasured experiment. Its claims were verified here before
acceptance — #3521 read in full, and the safe-versus-anonymous quote located on #3745 rather
than #3781 where it was cited, which turned up the sharper argument against my own position
(*"restricting the caller set SHRINKS the anonymity set"*).

**Reviewed live by the app tab session before publication**, which corrected four things in
the draft: the relayed "video is done" premise and the PR #167 disposition built on it;
"dissolved" downgraded to "bounded" on finding 2 with two named residuals; the Decision 1c
inversion moved from *accepted* to *put to Nick with both positions*, on the grounds that two
Claude tabs concurring is corroboration, not verification; and #3745 kept separate under its
own (different, better) characterisation. Every load-bearing claim it made was verified
against the app's source here before being accepted — `ring_controller.dart`, design 16 §2,
and `feature_flags.dart` — which is the same courtesy it extended in refusing to let this
document close #167 on its say-so.
