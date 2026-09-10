# TEMPER.md — Design 14, two transports one abstraction

**Overall verdict: DISSOLVE (3 of 4 families, unanimous among adversaries)**
**Struck:** `dt-172`, families seated: Maxwell + Kelvin + Carnot + Tesla (Wu disabled). Full 4-way — the 7KB bundle seated every family, where the 300KB code diff had darkened seats for six rounds.

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | RECAST | Q2 mis-framed so a review-packaging decision silently answered a shipping question; Q3 is a mechanism looking for a problem |
| Kelvin (Gemini) | **DISSOLVE** | "You cannot build a stable system on a foundation of *do this, but if you do, I crash*" |
| Carnot (GPT) | **DISSOLVE** | "FCM is not a transport yet; it is a future integration branch wearing a production nameplate" |
| Tesla (Grok) | **DISSOLVE** | "The guard that makes FCM unrepresentable makes APNs unbootable" |

Maxwell's RECAST named the same headline flaw the three DISSOLVEs did — that "don't ship FCM at all" was buried in a subordinate clause — and stopped short of following it. Three independent families did not stop short. **Recorded rather than retro-edited**, because a strike changed after reading the others is not an independent strike.

## Fatal flaws (deduped, most-severe first)

1. **The FCM boot refusal is a GLOBAL availability hazard, not containment.** *(Tesla; MEASURED and CONFIRMED)*
   Setting `FCM_SERVICE_ACCOUNT_JSON` does not disable Android — it makes the **entire island unbootable**, taking the working APNs transport down with it, under `restart: always`, with no FCM preflight. Verified directly: an APNs island that boots clean refuses to boot the moment the fifth credential is present.
   The design note priced this as *"operator-instruction contradiction"*. It is **global loss of the ring path that actually works**. Six code review rounds never named it; the design strike found it in 80 seconds, because it is invisible in a diff and obvious in a posture.
   **DISPOSITION: fold — the boot refusal must be replaced, not documented around.**

2. **Wrong option-frame: a half-transport was admitted to a live closed set.** *(Tesla, Carnot, Maxwell)*
   `Platform` is a closed enum. Admitting FCM to it — and to the credential tuple, the remedy map, the fixture matrix, the reaper — **demands an answer at every address**, while FCM remained "a rumor of a capability". Tesla's reading explains the finding curve better than the note did: six rounds of *same family, different address* was that demand being invoiced, not a design mystery.
   **DISPOSITION: fold — FCM leaves the shipped surface.**

3. **Premature unification of unequal evidence.** *(Kelvin, Carnot, Maxwell)*
   `ReapOrder` equates APNs's time-bounded proof with FCM's undated report, so the weaker, destructive reading became what silence constructs. Carnot: abstractions are *earned by duplication that survives contact with reality*, not predicted from two unlike protocols. With FCM out, the shared type has one user and the question dissolves without being answered.
   **DISPOSITION: fold — reframe as APNs-specific until a second real transport earns it.**

4. **`verdict=delivered` is a provider fact masquerading as a product fact.** *(Carnot)*
   200 means FCM accepted the message. The island's alarms treat it as *the handset was woken*. With no consumer, that is a meter reading on an open circuit — and the `delivered_to=0` alarm goes quiet precisely when it should scream.
   **DISPOSITION: fold — separate provider acceptance from product delivery in the model.**

5. **A cross-repo capability claim lives inside one repo's boot path.** *(Maxwell)*
   "The Android app has no receive half" is hard-coded in `config.py`. When the app ships that half, an operator on an older image is refused a credential their client can now use, with no mechanism to learn the claim expired.
   **DISPOSITION: fold — any readiness gate must be able to learn it is stale.**

6. **Q3 was a mechanism looking for a problem.** *(Maxwell; Carnot disagrees and wants the derivation)*
   Proposing a generator for operator guidance because prose drifted four times, when a rendered-output test already caught the live instance. **A genuine split between families — recorded, not resolved.**
   **DISPOSITION: named tradeoff — the rendered-output test stands; derivation is not adopted on this evidence.**

## What holds

- `ReapOrder`'s default constructing the destructive reading from silence is a real design-level finding; removing the default was right and survives.
- The APNs half — VoIP topic, push-type/topic fork, the reaper's dated authority, the token-kind routing — is not implicated by any DISSOLVE. Every family kept it.
- The evidence table in the note was honest and specific; nobody disputed a single instance.

## Disposition

**DISSOLVE at ≥2 families → the candidate is invalidated. Do not re-cast this note.**

The answer the strike converged on is simpler than any question the note asked:

> **FCM is not a transport of this island yet.** Ship APNs. Reintroduce FCM as an end-to-end product slice — consumer, preflight, operator guidance, alarm semantics and reap authority decided together — not as a send path with credentials attached.

This is an honest negative result. It invalidates a design note, not the six rounds of code fixes: those found and fixed twelve real defects, all of which stand.
