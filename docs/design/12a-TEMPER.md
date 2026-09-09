# TEMPER.md — island design 12a, the answer to the app tab's CallKit findings

**Overall verdict: RECAST** (4/4 families struck, 4/4 RECAST, zero DISSOLVE)
**Struck:** `dt-1788000000`, 2026-09-09, against `c36cfbb`.
**Families seated:** Maxwell (Claude) + Kelvin (Gemini 2.5 Pro) + Carnot (GPT/Codex) + Tesla (Grok) — **4/4**. Wu (Kimi K3) disabled upstream.
**Bundle:** island 12a (target) + island 12 + app design 16 v2 @ `8ee5ded` + claude-tasks#3744.

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | RECAST | The lease reframe survives only because *ring* and *call* are different objects — which the document never says — and implementing it drags back the per-call state a 3/4 DISSOLVE killed. |
| Kelvin (Gemini) | RECAST | The spine rests on the momentary ring being a *rare* event, and the unsolved multi-device key-freshness problem is an un-costed transfer of that risk onto Apple's revocation trigger. |
| Carnot (GPT) | RECAST | No longer slag, but not yet reversible: the load-bearing moves still depend on unmeasured Apple behaviour, a semantic rename, and a deferred anonymity story the design keeps adding attributable artifacts to. |
| Tesla (Grok) | RECAST | The trilemma did not dissolve — it was recategorised into a cell Apple does not sell, and both tabs agreed because both *needed* a third cell. |

## Fatal flaws (deduped, most-severe first)

**1. The "momentary ring" is not a designed topology — raised by Tesla, with Kelvin and Carnot converging on its consequences. THIS IS THE SHARED PREMISE THE STRIKE WAS COMMISSIONED TO FIND.**
`reportNewIncomingCall` is an async RPC to SpringBoard. Swift can *decide* sustain-vs-retract before reporting; it cannot *enact* silence, and `reportCall(endedAt:)` races "unknown UUID" before completion against a full-screen flash after it. Report-and-immediate-end is the iOS 13 abuse pattern the must-report rule was written to kill — flaw 9 is not a ratio to tune, it is fleet-wide revocation of the right to ring anyone. Neither tab could see this because both needed a third cell to keep device-local consent and VoIP in the same design.
**DISPOSITION: fold.** Strike the three-outcome table as a *designed topology*. The true statement is narrower: the device may decide sustain-vs-retract before report; it cannot silence; retract is an async race and a flaw-9 input, not a product cell.

**2. Arm (B) does not implement Nick's 2026-09-01 ruling, while wearing its name — raised by Tesla.**
"Default-off for groups, default-on for DMs with friends" under (B) is **VoIP for every DM invite, then retract**. A non-friend gets a DND punch-through and a Recents entry, not silence. An empty App Group (reinstall, OS wipe, Dart never written through) makes it *correlated*: every friend ring is report-and-end on the same morning and Apple hears one chord.
**DISPOSITION: fold, and surface to Nick.** Consent failures that must be silent need an island-side fact — arm (A), or a proxy the island already holds (group-vs-DM, blocked pair), never a new directory. If (B) stands, the document must say plainly that it is a deviation from the ruling and that report-and-end is the *malformed-push* path, not the consent path.

**3. The ring-lease reframe is a rename unless the wire carries the distinction — raised by Maxwell, Carnot and Tesla independently (3/4).**
You cannot expire a *delivered* push; `_EXPIRATION_SECONDS` is a pre-delivery TTL. A lease is a second island-minted wake asking CallKit to tear down UI the first push created, signed by no client — which is what Decision 5's last bullet forbids. **And it can murder a live conversation:** the island has no call object, cannot know the callee answered at T+8s, and at T+30s the stop fires. 12a never pins "stop ringing, not hang up", and never names a void-on-answer condition.
**DISPOSITION: fold.** Amend Decisions 1 and 5 **in the open**: the island infers exactly one fact, *ring timeout*, and still never infers hangup. Make it a distinct wire-level `ring_lease_expired` control wake with its own reason code — never forged as a signed client end, never occupancy, never revocation, idempotent, safe when racing a signed end. Pin the device-side no-op once answered. Nick's ruling is not the flaw; the claim that "Decision 1's boundary survives intact" is.

**4. The three clocks must collapse in THIS document, not later — raised by Maxwell, Carnot and Tesla (3/4).**
Invite APNs TTL 60s, lease 30s, freshness 10s. A push landing at T+45s is legal under the TTL and starts a ring whose stop has already fired into a phone that was not ringing — the inverted failure design 12 exists to prevent, regenerated. **Design 12 already wrote the fix** (*"a VoIP push should expire exactly when the ring stops; the two collapse into one number"*) and 12a left the collapse unowned.
**DISPOSITION: fold, CONDITIONALLY.** One invariant table with derivations, not constants: `apns-expiration ≤ lease = ring duration`. A ceiling whose stop can fire before the invite lands is not a ceiling.
**AMENDED 2026-09-09 by measurement ([`12a-MEASURED.md`](12a-MEASURED.md) M4):** the collapse contradicts a *written rationale in live source* — `apns.py` says 60s is **deliberately** longer than the app's 10s because *"the two clocks answer different questions."* Both are right for their own architecture: in the alert world a late wake is still useful, in the CallKit world it is a stop that cannot retract a ring not yet started. **The collapse is correct only under CallKit, which the system is not running.** Fold it as part of the CallKit transition, and retire the alert-world rationale explicitly rather than contradicting it silently.

**5. "No new transport" is false, and Decision 2 already disproved it — raised by Tesla and Carnot.**
A user who declines notifications has a VoIP token and will **never** have an alert token — legitimate, default, not a race. If ends and lease-stops ride alert, the ceiling Nick just assigned cannot reach the ringing phones that most need it. The island-owned ceiling and the end sentinel are **the same transport question**.
**DISPOSITION: fold.** Delete the no-new-transport claim. Keep the predicate name and the one-handset `endedAt` experiment as the gate; until it runs, both mechanisms are unspecified transport.
**AMENDED 2026-09-09 by measurement ([`12a-MEASURED.md`](12a-MEASURED.md) M1-M3): the finding is PREMATURE, not false, and its disposition is STRENGTHENED.** `grep -rni voip --include="*.py" src/` returns **2**, both inside one comment saying why we do not use it (positive-controlled: `grep -c device devices_service.py` → 14). `apns-push-type: "alert"` is a hardcoded literal; `Platform = {APNS, FCM}` is iOS-vs-Android, not alert-vs-VoIP. **No VoIP token exists, so flaw 5's population does not exist yet.** You cannot correct a predicate that forks a transport the system has never sent.

**6. Arm (C)'s unlinkability is not low-confidence — it is already spent — raised by Tesla, with Carnot and Kelvin converging.**
The island stores an attributable invite on a named channel and must hold the device token to wake it. Blindness over a capability cannot un-name a DM, and on a self-hosted island the anonymity set is a household, not a crowd. Kelvin adds a separate correction: the mid-ring revocation argument does **not** discriminate (B) from (C) — #3521 exists *with* device-local consent — so the honest disqualifier is the control inversion, not #3521.
**DISPOSITION: fold.** Replace "low confidence" with the flat statement that unlinkability is unavailable in this architecture. Move (C) to a research appendix. **Remove it as the named flaw-9 escape** — Tesla: *"leave (C) loaded as the escape and production will grab it at the first Apple warning."* Flaw 9's actual medicine is fewer VoIPs that are not calls.

**7. The UUID contract is a theorem about a copier, not a contract — raised by Carnot and Tesla.**
"By construction" is vacuous: the island never mints and never reuses because it has no call object — it also never **checks**. An attacker placing a *live* CallKit UUID in their own signed invite is copied faithfully, collapsing two rings or stopping call N−1. And the two tabs disagree without noticing: 12a says the payload UUID *is* the call id; 16 v2 says Swift takes the id from the signed body, never the envelope — which must-report makes false, since the failure path has no trusted signed ULID.
**DISPOSITION: fold.** The only consistent contract: **always report the payload UUID; admit only if signed ULID == payload ULID; on mismatch end the already-reported id; never report a second id.** Equality is a *check*. Pin that the end-wake and lease-wake carry the same call ULID, and name client-side reuse of a live ULID as an open hazard.

**8. Multi-device key-set freshness is an un-costed risk transfer — raised by Kelvin, seconded by Carnot.**
16 v2 §1c's "accept and disclose" hands the user a failed call and hands the platform an unbounded contribution to the report-and-end ratio. Write-through closes only same-device consent.
**DISPOSITION: fold into the app tab's §1c** (their surface, our dependency): a technical answer, costed — silent push to wake sibling devices on consent change, or a verification grace window that avoids report-then-end.

**9. The arms were never re-derived under the narrowed question — raised by Maxwell and Tesla.**
The document dissolves the trilemma, states the question *changes* to "what is a momentary ring worth", then keeps arms A/B/C verbatim because "the costing still stands". They answer the superseded question. Under the new one the natural arm-set is rate-limiting, reputation, and report-and-end budget — none of which appear. The document catches #3744 doing exactly this one section later.
**DISPOSITION: fold or delete.** Re-derive, or say the arm-set is owed.

**10. Enforcing the lease requires the per-call state #3170 dissolved — raised by Maxwell.**
Retaining call-id → device → sent-at → end-seen for 30s is a `live_calls` row with a different name. Nick's ruling settles *who owns the ceiling*; it does not license resurrecting the dissolved mechanism to implement it.
**DISPOSITION: fold.** Either show a stateless construction or name the state, price it against the DISSOLVE's reasoning, and say what changed.

**11. The Recents option space may already be foreclosed — raised by Carnot. Bears directly on a question awaiting Nick.**
If every failed proof must first report a CallKit call using a cached name, the record may exist *before* product chooses the ring/record cell. "Decide later" may be an empty switch.
**DISPOSITION: measure before asking.** Establish whether `includesCallsInRecents` is per-call or provider-wide, whether a failed proof can avoid Recents, and whether `reportCall(endedAt:)` writes history. **Do not put the product question to Nick until the degrees of freedom are known.**

## What holds

- **The two internal contradictions found in design 12** — Decision 4 versus Decision 1c, and the call/not-call predicate misnaming — are real, source-verified, and were invisible to both tabs reading their own documents. All four families let them stand.
- **The narrowing of Decision 4's sentence.** "No on-device window to *silence*" is the true claim where "no on-device window to reconsider" was too strong. Kelvin: *"a time interval pretending to be a point."* Survives in weakened form even under Tesla's strike.
- **The predicate NAME** `ring-starting / not-ring-starting`, and the discipline of deferring the hardening behind a cheap one-handset experiment. Carnot: arguing past it *"would be design theatre."*
- **Keeping occupancy separate from the end sentinel.** Carnot: closing it would be *"a classic weaker-instrument substitution."* The withdrawal of the close-PR#167 recommendation is upheld by the panel.
- **The ratchet self-catch** — withdrawing the capability recommendation because an anonymity ruling is not authority over the consent axis.
- **The requirement that an island stop be distinguishable from a signed client end.** Tesla: *"the metaphysics of 'lease not end' are what fail, not the need for a distinguishable wake."*
- **Refusing to let two-tab agreement stand as a decision.** Three families noted it; it is why finding 1 reached Nick as a fork.

## Disposition

**RECAST.** No family voted DISSOLVE, so the candidate is not slag — but flaws 1, 2 and 3 are load-bearing and two of them (the momentary cell, and (B) not implementing the ruling) are **cross-repo**: they land on app design 16 v2 as hard as on 12a.

Round 1 of ≤3. Before round 2:

1. **Hand flaws 1, 2, 6 and 8 to the app tab** — they strike v2's §0/§1c/§1d, not just this document.
2. **Run the one-handset `endedAt` experiment** (flaw 5's gate) and the Recents degrees-of-freedom probe (flaw 11). Neither is decidable by argument.
   - **Flaw 11 is ANSWERED** (2026-09-09, read from the iOS 26.5 SDK): `includesCallsInRecents`
     exists **only** on `CXProviderConfiguration` — `CXCallUpdate` has no recents field, so it
     is not per-call via the update path. **But `CXProvider.configuration` is `readwrite`**
     (`CXProvider.h:114`), so the configuration can be swapped at runtime. The option space is
     **not** foreclosed at the API level; whether iOS honours a swap between report and end is
     the narrower device question already open as claude-tasks#3775.
   - **Flaw 5's gate genuinely needs a handset** — `simctl push` states outright that VoIP
     pushes are unsupported on the Simulator. It is **not** blocked on Nick: his iPhone is
     paired and available to this Mac. It is blocked on there being no CallKit/PushKit code in
     the app repo at all, so the experiment needs a ~50-line throwaway Swift harness first.
     A build task, not a permission task; the only Nick-shaped part is consent to deploy a
     probe to his personal device.
3. **Surface flaw 2 to Nick**: arm (B) may not implement his 2026-09-01 ruling. That is his to rule on, not ours to fold.
4. Then recast §Headline, Finding 1 and Finding 3, and re-strike.

**Nothing here is build-ready.** The standing gate from the app tab's v1 temper — *no `CXProviderDelegate` and no VoIP send path until the eligibility question is decided* — is reinstated by flaw 2, which reopens exactly that question.
