# 12a-RECAST — round 2 of the temper, and what measurement did to round 1

**Answers:** [`12a-TEMPER.md`](12a-TEMPER.md), overall verdict **RECAST**, 4/4 families,
zero DISSOLVE, struck 2026-09-09 against `c36cfbb`.
**Written:** 2026-09-12.
**Bundle read before writing:** island 12 + 12a + 12a-MEASURED + `push_service.py`/`apns.py`
as they stand today; app `16-callkit-ring-v2.md` @ `6bd4ab2` (**PR #196, still OPEN**), design
19, design 20, ADR-0008, and claude-tasks #4178, #4254, #4265, #4278.

---

## The one thing to read if you read nothing else

**Round 1's strike was argument. Round 2 is mostly arithmetic, because between the two a
handset answered four of the eleven flaws.** Three dispositions survive unchanged, four are
discharged by measurement, one is superseded by a later app-tab record, and one of the
*replacement arms round 1 prescribed* has been measured out of existence.

That asymmetry is the finding. A design temper is the right instrument for a shared premise and
the wrong instrument for a question a device can answer — and round 1 said so itself
(*"neither is decidable by argument"*). What it could not know is how much of its own reasoning
was standing on the undecided half.

---

## What moved since the strike

| # | Round 1 flaw | Status now | Why |
|---|---|---|---|
| 1 | The "momentary ring" is not a designed topology | **HOLDS — now measured, and the mechanism was wrong** | A report-and-end *is* perceptible (flash + buzz, measured blind). But it **counts as reported**, so it is not the must-report violation round 1 called it. |
| 2 | Arm (B) does not implement Nick's ruling | **FORK REFUSED — re-derivation owed** | Round 1 was right it was not ours to fold. The arms were all one cell of a two-cell space, with the third cell imaginary. |
| 3 | The ring-lease reframe is a rename unless the wire carries the distinction | **HOLDS, partially discharged** | The wire now carries `"k"` (v0.12.0). The *call-identity* half is untouched and is claude-tasks#4265. |
| 4 | The three clocks must collapse in THIS document | **HOLDS** | Still unowned. #4265 is its representation. |
| 5 | "No new transport" is false | **DISCHARGED** | Its gate ran (#4178, #4278) *and* its "premature" amendment expired: VoIP shipped in v0.11.0, so the population now exists. |
| 6 | Arm (C)'s unlinkability is already spent | **CLOSED (12a's arm C) / SUPERSEDED (design 20's)** | Two different arm Cs. 12a's capability is closed — its trigger is provably unmet. Design 20's sealed envelope is a later record on a different property. |
| 7 | The UUID contract is a theorem about a copier | **HOLDS, partially discharged** | `"k"`'s total function covers unknown/missing. The **equality check** is still owed. |
| 8 | Multi-device key-set freshness | **HANDED OFF** | App tab's §1c, their surface. |
| 9 | The arms were never re-derived | **HOLDS — and one prescribed arm is now void** | Round 1 prescribed "rate-limiting, reputation, **report-and-end budget**". There is no budget. |
| 10 | Enforcing the lease needs the state #3170 dissolved | **HOLDS** | Untouched. The sharpest open item. |
| 11 | The Recents option space may be foreclosed | **ANSWERED** | Inside round 1 itself, from the iOS 26.5 SDK. |

---

## The measurement that did the most damage, and it damaged round 1 in its favour

**claude-tasks#4278, 2026-09-12, iPhone 14 Pro / iOS 26.6.1, sandbox APNs.** Harness, sender
and raw log committed (`spike/voip-must-report-endlive`, `1f3b3c1`).

Three results, and the middle one rewrites round 1's flaw 9:

1. **`reportNewIncomingCall` followed immediately by `reportCall(endedAt:)` COUNTS AS
   REPORTED.** Seven consecutive, no plain report between, pid unchanged.
2. **Must-report is a CONSECUTIVE-violation counter that any successful report RESETS.** Four
   consecutive silent pushes terminate; five interleaved with reports never do.
3. **A report-and-end is PERCEPTIBLE** — a brief flash and buzz, measured blind (assignment
   coin-tossed into a file that was never printed, description taken before the reveal).

### What this does to flaw 9

Round 1 wrote: *"flaw 9 is not a ratio to tune, it is per-device DENIAL of VoIP delivery."* It
then prescribed a replacement arm-set of *"rate-limiting, reputation, and report-and-end
budget."*

**There is no report-and-end budget, because a report-and-end is not a violation.** Result (1)
removes the thing the arm would have metered, and result (2) removes the accumulation it would
have bounded — normal call traffic *cannot* accumulate, since every end wake is preceded by an
invite wake that reports successfully.

So flaw 9's own disposition is now: not a ratio, not a budget, and **not a denial risk under
normal traffic at all**. What remains is a **user-experience cost** — one buzz per hangup —
which is a product question, not an engineering unknown.

> **The kill counter is soft; the reset is hard.** #4178 saw termination on the *second* push,
> #4278 on the fourth; the counter is device-local and carries history
> (`CSDVoIPApplicationKillCounts`). Never treat the number as a budget to spend. What survives
> is the reset.

### And the one branch that is still dark

`reportCall(with:endedAt:)` **alone** retracts a live ring — proven by a *missing* event: an
unanswered CallKit ring self-expires at ~60s, ten of twelve `report` pushes fired
`CXEndCallAction` exactly 60s later, and the only two that did not are the two rings the
`endlive` pushes ended.

That makes `endedAt`-alone look like a strict improvement for the live-ring case: no report, no
buzz, ring gone. **The must-report status of `endedAt`-alone against a LIVE ring is
UNMEASURED.** #4178 proved it fails for an id iOS has never seen — the *stale* case — and that
arm ran interleaved, so it could observe retraction and was structurally incapable of observing
reporting.

**The island has no position to take here except one: the rule stays unconditional, and our
reason is not the app tab's.** An unconditional rule cannot take the bad branch. A conditional
one is only as good as the liveness check selecting it — and that check reads state **a
throttled or hostile island controls**, which is the next section.

> **Correction to this repo's own record.** `project_v0120_end_sentinel.md` carried
> *"`reportCall(endedAt:)` alone does NOT satisfy must-report"* flat, while
> `concept_control_cannot_produce_the_failure_it_screens_for.md` recorded six inches away that
> *"that race has never met a handset."* Two of our files, one contradiction, nobody editing a
> line to create it — the narrow measured fact restated one notch stronger on its way into the
> project record. Narrowed 2026-09-12.

---

## NEW, and it post-dates the strike: the island manufactures lone ends

Not a round-1 flaw. It is the island-side input that would select the dark branch above, and it
is on our side of the wire.

**Measured in source** (`push_service.py:423`):

```python
allowed, _ = limiter.hit("apns_wake", user_id,
                         settings.apns_wake_per_recipient_per_minute, 60.0)   # default 6
```

**One bucket, keyed on the recipient, six per minute, with no notion of a call.** An invite wake
and its end wake are two independent charges against it.

**State the mechanism narrowly, because the handoff that raised this stated it too broadly.**
This is not a systematic asymmetry in which ends outrank invites — an exhausted budget drops
both. It is that **the pair is not atomic**, so the two halves can land on opposite sides of a
fixed-window boundary: an invite refused as the 7th wake at t=59s, its end admitted as the 1st
wake of the next window at t=61s. The same shape arises whenever the invite is dropped for a
reason the end is not — no registered device at invite time, an expired APNs TTL, a device that
registered in between.

The result is a `k == "call_end"` wake for a call the handset never saw.

**Three consequences, in increasing order of how much they are ours:**

1. **A spurious momentary ring.** Under the client's total function the handset reports and
   immediately ends — a buzz for a call that never existed. Survivable, but it is noise we
   generate.
2. **A privacy signal the app tab already conceded independently.** §7c: *"a LONE `call_end`
   with no preceding invite does tell Apple something timing alone would not."* Two tabs
   reached this from opposite directions without coordinating.
3. **It is the input that makes a conditional client branch dangerous.** A client that
   optimised the live-ring case to `endedAt`-alone would take that branch on exactly these
   wakes, where there is no live ring — the stale case #4178 proved is unreported.

**DISPOSITION: fix the coupling, not the window.** The budget's unit is wrong: it meters
*wakes* while the thing being rationed is *interruptions*, and a call is one interruption that
costs two wakes. The candidate shapes, cheapest first:

- **Pair the charge.** A call's end is charged with, or reserved by, its invite, so an end is
  never admitted independently of the invite it belongs to.
- **Drop the lone end at the send door.** If the island did not send the invite, it does not
  send the end. This needs the island to know which invite an end belongs to — which is
  **#4265**, the same representation gap, which is why the two are one item and not two.
- Tell the app tab and let the client refuse. Rejected as the primary: it moves an island-made
  problem across a trust boundary, and the refusal would live in the branch we just argued must
  stay unconditional.

Owed as a ticket, not built here.

---

## Flaw 2 — the fork was REFUSED, and the refusal is the finding

Round 1: *"Surface flaw 2 to Nick: arm (B) may not implement his 2026-09-01 ruling. That is his
to rule on, not ours to fold."* Surfaced 2026-09-12 with the buzz measured rather than feared.

**He ruled an arm, then asked the question that dissolved the fork:** *"are we going down the
path of allowing the island to know who's friends with whom?"* On the re-put he took none of
the arms. **The arms all accepted that a stranger's invite reaches the VoIP path, and argued
only about how to undo it afterwards.**

### THE STRUCTURAL RESULT — there is no third cell, and this is why every arm slid

Two facts, both already established in this bundle, neither previously put side by side:

1. **In a DM, the channel IS the pair.** A DM channel is a two-party object; naming it names
   both ends. (`_gated_dm_channel` enforces exactly this: `len(peer_ids) != 1` is a 403.)
2. **iOS has no silent VoIP.** Every PushKit delivery must be reported to CallKit before the
   handler returns. *You can foghorn a message; you cannot foghorn a ring.*

Therefore:

> **Any island-side ring/no-ring selectivity WITHIN DMs is the friend edge, by construction —
> regardless of how it is derived, what it is named, or how careful the derivation is. And any
> device-side selectivity is a buzz, by construction, because the push already arrived.**

**Those are the only two cells.** The third cell — *silence for a stranger, without the island
knowing the pair* — does not exist on this platform. Every proposal that promised it was
smuggling the edge in under another name:

| proposal | the smuggle |
|---|---|
| 12a arm (A) "publish the consent fact" | explicit; 12a rejects it on ADR-0004 |
| "gate on proxies the island already holds" | the gradient: group-vs-DM → *have they ever messaged* → the edge |
| a per-channel ring opt-in | in a DM, a per-channel fact IS a per-pair fact |
| 12a arm (C), blind-signed capability | withdrawn by 12a; and #3745: *restricting the caller set SHRINKS the anonymity set* |

This is the same shape as the trilemma round 1 said did not dissolve but *"was recategorised
into a cell Apple does not sell"* (Tesla) — recurring one level down, on the consent axis
instead of the ring axis, and **both tabs wanted the third cell again for the same reason: it
is the only cell where the product is nice.**

### Arm (C) is CLOSED, not parked

12a held (C) as *"the named escape if flaw 9 measurement says the report-and-end ratio is
untenable."* The measurement says **there is no ratio** — a report-and-end counts as reported,
and must-report is a consecutive counter that any successful report resets. **(C)'s trigger
condition can no longer be met.**

Round 1's warning is the reason to close rather than park it: *"leave (C) loaded as the escape
and production will grab it at the first Apple warning."* A parked arm with a dead trigger is
precisely what gets grabbed, because nobody re-reads the trigger.

### The question that was never asked, and is now the live one

**Why is a stranger's DM invite on the VoIP path at all?**

Both arms took VoIP-for-every-DM-invite as the given and negotiated the cleanup. But the island
has held a transport fork since v0.10.0 — `token_kind` is `VOIP` or `ALERT` — and an alert push
is not a CallKit report: no buzz, no Recents entry, no must-report obligation. A stranger's call
arriving as *"X is calling"*, tappable, is not a degraded ring; it is a different and arguably
more honest product.

That reframes the grade as **the recipient's interruption policy**, which is what the friends
crucible already named and nothing built:

> **Graded reachability** (Carnot's spark): `urgent` / `ring` / `glow` / `silent`, **set by the
> recipient**, per tie, per direction. *"Real product value, zero machinery — the piece worth
> building first regardless."*

**And the structural result binds it too, which is the honest part.** `ring`-vs-`glow` enforced
island-side, per DM, is the edge. So graded reachability does not escape the two cells — it
**changes what the cells cost**:

- the island-side cell stops being "learn who is friends with whom" and becomes "learn which
  channels this user accepts rings in", which is a *notification preference the recipient
  authors deliberately*, correlates with friendship without being it, is revocable instantly by
  its owner at the enforcement point, and needs no distribution channel (the three objections
  that disqualified arm (C));
- the device-side cell stops costing a buzz-and-vanish and costs only that strangers arrive as
  notifications rather than rings.

**Whether that re-pricing is enough to change the answer is NOT decided here, and this document
declines to pick.** What it fixes is that the previous fork was a choice between two cells with
one of them mispriced and the third one imaginary.

**OWED: re-derive the arm set under "what is the recipient's interruption policy, and who
enforces it", not under "how do we undo a ring we already sent."** That is a design pass, not a
paragraph.

## Dispositions that survive round 1 unchanged

**Flaw 3 — the lease is a rename unless the wire carries the distinction. STILL LIVE.**
The wire now carries `"k"`, so an end is no longer mistakable for an invite. But round 1's
actual demand was a *distinct control wake with its own reason code, carrying the call ULID*,
and what shipped carries a **channel**, not a call. The hazard round 1 named — *"it can murder
a live conversation: the island cannot know the callee answered at T+8s, and at T+30s the stop
fires"* — is untouched and is **#4265**. Round 1's sentence stands verbatim.

**Flaw 4 — the three clocks. STILL UNOWNED.** Invite TTL 60s, lease 30s, freshness 10s. Round
1's conditional amendment (M4: the collapse contradicts a written rationale in live source,
because `apns.py` says 60s is *deliberately* longer, and both are right for their own
architecture) still applies — with one change: **the CallKit transition it was conditioned on
has begun.** VoIP shipped in v0.11.0; the end sentinel and its interlock in v0.12.0/v0.12.1. The
collapse is no longer hypothetical, and the alert-world rationale must be retired explicitly
rather than contradicted silently.

**Flaw 7 — the UUID contract. PARTIALLY DISCHARGED.** `"k"`'s total function pins the
unknown/missing row (report, then immediately end, never sustain), recorded in the island's
`WakeKind` source as a shared invariant. Round 1's *equality check* is still owed: **admit only
if signed ULID == payload ULID; on mismatch end the already-reported id; never report a second
id.** "By construction" remains vacuous until something checks.

**Flaw 10 — the lease needs the per-call state #3170 dissolved. UNTOUCHED, and now the
sharpest item on the list.** Everything above that resolves — pairing the wake charge, dropping
a lone end, naming the call in the wake — wants the island to hold *something* per call. Round
1's demand is the right gate and it has not been met: **either show a stateless construction,
or name the state, price it against the DISSOLVE's reasoning, and say what changed.** This
document does not meet it and does not pretend to.

---

## Superseded, and surfaced rather than folded

**Flaw 6 — arm (C).** Round 1: *"Replace 'low confidence' with the flat statement that
unlinkability is unavailable in this architecture. Move (C) to a research appendix. Remove it
as the named flaw-9 escape."*

**The app tab has since accepted arm C** (design 20, *the sealed ring envelope*, and
`3057d16` *"arm C accepted — Decision 6's harm is the reader"*) — on a **different property**
from the one round 1 disqualified. Round 1 struck arm C's *unlinkability against the island*;
design 20 claims a property about what **Apple** learns, and names the reader as the harm.

Per this repo's CLAUDE.md, *a tab's own later record outranks an earlier cross-repo handoff
answer*, and *when the record and your memory disagree, that is a finding — surface it, do not
tie-break*. So:

- Round 1's disqualifier **is not withdrawn**: unlinkability *against the island* is
  unavailable in this architecture, and on a self-hosted island the anonymity set is a
  household.
- Design 20's claim is about a different adversary and is **not contradicted** by that.
- **The live risk is round 1's real warning, and it survives the supersession:** *"leave (C)
  loaded as the escape and production will grab it at the first Apple warning."* Arm C must not
  be reachable as a flaw-9 escape — and flaw 9, per the measurement above, no longer needs an
  escape.

Not folded. Owed as a cross-tab item: **which property is arm C being built for, and is it
load-bearing for anything in the ring path?**

---

## Still owed after this recast

| Item | Owner | Ticket |
|---|---|---|
| The call has no identity on the wake — the wake names a channel | island | #4265 |
| Pair the wake charge, or drop the lone end | island | [#4325](https://github.com/nickmeinhold/claude-tasks/issues/4325) (one item with #4265) |
| The equality check on payload ULID vs signed ULID | island + app | [#4327](https://github.com/nickmeinhold/claude-tasks/issues/4327) |
| The three clocks: one invariant table with derivations | island | (owed, flaw 4) |
| Stateless construction, or price the per-call state | island | flaw 10 / #3170 |
| Arm C — which property, and is it in the ring path? | cross-tab | (owed) |
| Multi-device key-set freshness | app | their §1c |
| Re-derive the ring grade as the recipient's interruption policy | island + app | [#4326](https://github.com/nickmeinhold/claude-tasks/issues/4326) (re-scoped, Nick 2026-09-12) |

**What this document is NOT.** It is not a decision of record for anything except flaw 2, which
is Nick's and is attributed. It does not meet flaw 10's gate. It builds nothing. It rests in
places on app design 16 v2, which is **PR #196 and still open** — its *measurements* are facts,
its *design positions* are proposals, and this document treats them differently on purpose.

## Provenance

Round 1's correction note is worth re-reading before trusting anything here: *"fleet-wide
revocation"* entered as temper wording, was restated four times across two repos, and survived
four adversarial families **because it was never written as a claim** — it arrived as background
colour inside an argument about something else.

This recast contains at least one instance of the same shape, caught and named above: this
repo's `project_v0120_end_sentinel.md` restated a narrow measured fact as a wide one, while a
sibling memory file recorded the gap. The lesson round 1 drew is the one to keep — **the error
is in the restating, not in the measurement** — and the raw strike files in
`temper-strikes-12a/` remain deliberately unedited for the same reason.
