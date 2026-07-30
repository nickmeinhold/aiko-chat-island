# Blade — Phase A implementation plan (the tempered blade)

*Movement 6. The crucible's terminus: the tempered, converged design translated into an
ordered plan. Phase A only (legal-independent, re-Temper-certified). Phase B (MLS +
franking + signed-room-policy enforcement) is trigger-gated on federated-MLS maturity +
Matt's legal answer. Blade hones; it did not re-forge — no third cage-match.*

## Scope: Phase A = the honesty layer (Moderator-only)

Phase A ships NO encryption. Its entire value is making every island's *current*
operator-readability **honest and legible** to users, and closing the dangerous middle —
which is real safety/consent value even if Phase B never lands. `e2ee` is schema-reserved
and hard-rejected until Phase B.

## Ordered steps (each independently useful, core-first)

**A1 — Island `mode` as a signed manifest property.**
Add `mode` to the island's signed identity/manifest. Enum `moderator | e2ee`; in Phase A
only `moderator` is valid. Default `moderator` (matches the two live islands' reality).
Sign it with the island key (it's a trust-boundary claim). Expose via the island identity
endpoint. *Ships:* the honest declaration exists on the wire.
*Files:* island manifest/identity model + signing; a migration for the stored field.

**A2 — `e2ee` hard-reject invariant (structural, not a note).**
Production boot AND the federation handshake REJECT any `e2ee` mode value with a clear
"non-production / Phase B" error. This is the invariant that prevents the mislabeled-mode
worst case (users believing E2EE while the gateway reads plaintext). *Ships:* the false-
assurance vocabulary is structurally impossible in Phase A.
*Files:* boot validation + handshake validation.

**A3 — Client legibility (app-repo).**
The client fetches the island mode and displays it BEFORE the user speaks — e.g. "This
island is operator-moderated: your messages (including DMs) are readable by the operator."
Unmissable, not buried in settings. *Ships:* the consent win lands for real users — the
core point of Phase A.
*Files:* aiko_chat_app — connect flow + a per-island mode banner.

**A4 — Mode carried in the federation handshake (declaration, not filtering).**
Include the signed mode in the peer federation handshake so peers OBSERVE each other's
mode. NO filtering logic yet (there's only one mode to see in Phase A — filtering is a
Phase B no-op-until-then, per the re-Temper). *Ships:* the foundation mode-aware
federation builds on later, without shipping a filter over a non-existent distinction.
*Files:* federation handshake wire format (additive field).

**A5 — Moderator = commitment.**
Wire the election so `moderator` REQUIRES the report queue + retraction present and a
CSAM-runbook acknowledgement at election, and CANNOT be disabled without a mode change.
Enforces capability-present (not duty-discharged — stated honestly, not oversold).
*Ships:* the "plaintext with moderation deleted" config is structurally forbidden on the
live islands. *Files:* the moderation/report subsystem + the mode-election path.

## Deferred to Phase B (trigger-gated — do NOT build in Phase A)

Signed room/epoch encryption-policy ENFORCEMENT (client-verify-before-send), MLS group
encryption, committing-AE message franking, mode-aware federation FILTERING, device/member
transparency (binds to note 06 KT log). Triggers: federated-MLS maturity + Matt's legal
answer (task #7). The room/epoch policy is designed as a protocol invariant now (A1's mode
field is its seed) but its E2EE enforcement is Phase B.

## Blast radius & the build gate

Trust-boundary (signed manifest) + wire-format (handshake) + state-lifecycle (mode
election) → **the CODE PR gets a `/cage-match` by law** (CLAUDE.md), separate from this
design temper. Phase A touches no encryption, so crypto risk is low; the signed-mode claim
is the main trust surface. Backend-first, single-door (seal the mode in the mutator, not
each caller). Ship A1→A5 as one PR or a small stack; A3 is the app-repo half.

## Honest state at Blade

Architecture converged (re-Temper round 2, both adversaries: Phase A Blade-ready).
This plan is buildable independent of Matt. The BUILD is a fresh effort (a future
session), not started here. Phase B resumes when the two triggers clear.
