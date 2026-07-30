# Temper — cross-family design cage-match

*Movement 5. 2026-07-30. Verdict: **REQUEST_CHANGES** (3 of 3 available adversaries,
convergent). Wu/Kimi quota-dead (403) → 3-way adversarial (Kelvin/Gemini, Carnot/Codex,
Tesla/Grok) + Maxwell. Legal axis un-tempered by design (lawyer question). The design
does NOT survive clean — it needs a re-Cast (findings folded into DESIGN.md) plus one
product decision that is Nick's, not the reviewers'.*

## Convergent findings (all three families, independently)

1. **FATAL-but-fixable — federation makes channel mode ambiguous → downgrade attack.**
   The per-island election is underspecified at the relay layer. Tesla's decomposition:
   three things are called "channel" — room state (home island), **message
   delivery/relay path (every relay's mode — NEVER sealed by the Cast)**, client
   encryption decision. The gap opens a downgrade surface (a Moderator peer demands
   plaintext "for compliance"; or silently relays an "E2EE" room as plaintext — Matrix's
   known bridge/bot killer). **Converged fix:** a room has exactly ONE encryption policy,
   fixed at creation (or an explicit rekeyed epoch), bound to room-id + creator signature
   + membership epoch, enforced as a **federation JOIN PREDICATE** (accept the policy or
   cannot join; no peer flips ciphertext→plaintext; relays never re-encode; client
   verifies cryptographically before sending, fail-closed). Plus Carnot's four-concept
   split: island-hosting-mode / channel-effective-mode / per-peer-relay-policy /
   client-verified-send-mode are FOUR distinct things.

2. **Per-island is the right LIABILITY atom, the wrong ENCRYPTION atom.** "Encryption
   wants room (or epoch); liability wants operator." And a **silent product premise** the
   design never states, which decides whether per-island is even right: is aiko
   *one-community-per-island* (per-island coherent) or *multi-community-federated-on-one-
   box* (per-island fails — a solo op wanting a public square AND a private ops room is
   told "run a second island," a non-starter)? → **Nick's product call.**

3. **Phase A must not advertise E2EE before Phase B.** Shipping a `mode` enum with a
   selectable/advertised `e2ee` before MLS+franking exist is the exact mislabeled-mode
   worst case (users believe E2EE, operator reads). Fix: `e2ee` unselectable /
   non-production in Phase A; the only honest claim is "Moderator/plaintext."

4. **"Moderator = commitment" kills undeployed machinery, not human neglect.** Enforces
   capability-present, not duty-discharged; theater if sold as "Moderator means
   moderated." Fix: rephrase to exactly what it enforces (report queue + retraction
   present, cannot disable without a mode change); do not oversell.

5. **Immutability is too strong — a data trap (Kelvin).** Mode-immutable-at-creation
   traps a Moderator community in plaintext forever. Sovereignty is continuous. Fix:
   **forward-only epoch transition** — an operator may seal a Moderator island to E2EE
   *going forward* (old history stays readable, cannot be retroactively encrypted); the
   reverse (E2EE→Moderator) is forbidden (can't retro-decrypt). Not free immutability;
   a monotone one-way ratchet toward MORE privacy only.

## Disposition

- Findings 1, 3, 4, 5 → **folded into DESIGN.md** (re-Cast, round 1 of ≤3).
- Finding 2's product premise → **escalated to Nick** (one-community-per-island vs
  multi-community-per-box); it gates whether per-island survives at all.
- Legal axis → **task #7 / Matt** (un-temperable by review).

## Do NOT proceed to Blade yet

The design didn't survive clean, and two of its load-bearing questions are external
(Nick's product premise + Matt's legal answer). A plan built now would be planning on an
un-decided frame. Re-Cast is committed; the re-Temper + Blade wait on the two external
decisions. Honest state: **strong ore, real fixes applied, two external gates open.**

## Panel note

3 of 4 adversary families seated (Wu/Kimi hit a 403 quota limit this billing cycle — an
availability gap, not an approval). Convergence across three *different* families on
finding 1 is the strongest possible signal that it's real, not a single instrument's
bias.

---

## Re-Temper (round 2, 2026-07-30, neutral prompt — no steering)

Verdict: **REQUEST_CHANGES but CONVERGING** — Carnot + Tesla (Kelvin SIGTERM'd by a
2-min shell timeout; Wu still 403 quota-dead → 2-way + Maxwell). Both adversaries
explicitly: "fix these three → APPROVE-ready for a Phase A Blade." Convergent findings,
all folded into DESIGN.md Re-Cast round 2:

1. The finding-2 fold OVERCORRECTED — saying per-island is "both liability AND encryption
   atom" re-collapsed the round-1 fix. Correction: per-island = default that auto-creates
   the room policy; the signed room/epoch policy stays the ENFORCED invariant, client-
   verified before every send incl. remote joins.
2. "E2EE self-verifying" overstated — narrowed to "a trustworthy client can verify
   encryption to the displayed MLS group"; requires MLS credential verification + device/
   member transparency (= note 06's KT log) + fail-closed send. Client integrity + KT, not
   "self-evident".
3. Phase A HARD-rejects e2ee at handshake (invariant); relay/bridge/export + partition-
   healing are invariants not operator policy; one-community-one-posture holds only with
   ZERO exceptions (no except-DMs/admin/bridge).

**Architecture converged.** Both adversaries: core dichotomy sound, Phase A Blade-ready
post-fold. Stopped at round 2 of ≤3 (diminishing, bounded, agreed findings). Blade waits
only on the legal axis (Matt / task #7); Phase A is Moderator-only + the honest election
framework, so the legal answer shapes the runbook, not the Phase A mechanism.
