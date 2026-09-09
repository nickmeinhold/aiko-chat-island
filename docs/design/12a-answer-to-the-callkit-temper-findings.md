# Design 12a — the island's answer to the app tab's CallKit temper findings

**Answers:** claude-tasks#3744 (four findings the island half owns), filed by the app tab
2026-09-01 from its 4/4 `/design-temper` on app design 16.
**Amends:** design 12 (`12-native-call-ui-callkit-connectionservice.md`) — Decisions 1c, 4, 5.
**Status:** the island's position, written to be struck. Not a decision of record until it is
tempered and, where marked, until Nick rules.

---

## The headline: a three-way contradiction, stated by neither tab

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

**I recommend (C), and it is not a new idea I am importing** — it is the direction already
ruled in. Nick, 2026-08-25: the island should learn neither who is friends with whom **nor
who is calling**; blind wake tokens are how a ring is spent without a session. (C) is that
mechanism doing a second job it was already shaped for.

**Two guardrails on (C), because this claim has inflated once already.** Its ruled scope is
sender-side: *"the island cannot link a ring to an account in its own data."* It does **not**
buy recipient anonymity — the island must hold the device token to send the wake, so it knows
who is being woken, and that survives at N=33 because the binding constraint is APNs, not
scale. And it is void at the IP layer without a mixnet: that remains an operator promise
about logging, not a property of the system. Anything stronger is an overclaim.

---

## Finding 1 — Decision 1c may be assigning the app tab something it cannot hold

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
  fleet-wide.

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

**The open half is the one the app tab flagged and I am not closing tonight:** an alert push
does not wake a locked ringing phone reliably, which is the inverted failure Decision 5
exists to prevent. So the corrected predicate is necessary and may not be sufficient. That
needs measurement, not reasoning — see the owed work below.

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

- **Measure it, do not reason about it.** Does an alert push wake a locked, ringing handset
  in time to stop a CallKit ring? Finding 3's corrected predicate depends on it and no amount
  of design settles it. We have two real handsets and a live island.
- **#3745 stays a separate thread — do not fold it into this answer.** I had it filed as
  "two incompatible ring designs, neither citing the other"; the app tab corrected the
  characterisation and I take it. Its core is the **sender-anonymity contradiction**: designs
  12 and 16 build the ring as a stored, attributable message, while Nick's 2026-08-25 ruling
  says the island should learn neither who is friends with whom nor who is calling. Folding
  it into a resolved findings answer would bury an independent open question inside one.
  Sharpened further on that thread: design 12's answer to the liveness finding is *"cancellation
  is a signed client message"* — its fix for one problem is **more** attributability, which
  runs *against* the anonymity ruling rather than merely being silent on it.
- **Design 16 v2 (#3781) is not written; the app tab starts it tonight.** Nothing here cites
  it and nothing should predict it. It will cite this document and this document will cite it
  once it lands — that mutual citation is the actual fix for the two-designs problem, and it
  is separate from #3745's content.
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

**Reviewed live by the app tab session before publication**, which corrected four things in
the draft: the relayed "video is done" premise and the PR #167 disposition built on it;
"dissolved" downgraded to "bounded" on finding 2 with two named residuals; the Decision 1c
inversion moved from *accepted* to *put to Nick with both positions*, on the grounds that two
Claude tabs concurring is corroboration, not verification; and #3745 kept separate under its
own (different, better) characterisation. Every load-bearing claim it made was verified
against the app's source here before being accepted — `ring_controller.dart`, design 16 §2,
and `feature_flags.dart` — which is the same courtesy it extended in refusing to let this
document close #167 on its say-so.
