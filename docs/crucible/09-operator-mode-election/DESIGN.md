# Design: Operator mode election — E2EE xor Moderator

*Movement 3 (Cast). Draft for temper. Becomes `docs/design/08-*.md` if it survives.
Backed by RESEARCH.md (Heat) and note 07's two prior passes. Not legal advice.*

## Problem

Note 07 proved the moderation axis is a **true dichotomy** ([[concept_confidential_xor_moderatable]]):
a channel is either plaintext-at-the-gateway (moderatable + on-the-hook-to-scan, no
legal shield) or E2EE (confidential + shielded, unmoderatable). Crucible 08 proved the
"third point" (operator-blind enclave) is ~impossible. So aiko must **choose** — and the
sovereignty ethos says the choice belongs to the node that bears the legal risk, not to
the protocol author.

## Proposed shape

**Each island operator elects one mode for their island.** The mode is a signed
property of the island, surfaced to clients at connect and to peers at federation
handshake.

- **Moderator mode** (the shipped status quo, made explicit): gateway holds plaintext;
  the #7 takedown/retraction machinery + report queue operate; operator carries the
  scan/report legal duties (note 07 Part B runbook). **Electing this mode is a
  COMMITMENT** — it turns the moderation subsystem ON; there is no "plaintext but not
  moderating" resting state (the worst legal spot: liability without the safety work).
- **E2EE mode**: channels are client-encrypted on **MLS (RFC 9420)**; the gateway holds
  only ciphertext and *cannot* moderate (MLS guarantees this even against a compromised
  server). Safety is **client-side**: users report a specific message via **committing-AE
  message franking** (distinct mechanism, §build-4), which lets the operator receive a
  provable report without reading anything else. Group abuse uses asymmetric franking /
  traceback (with a named forwarding-metadata cost).

**Mode is per-ISLAND, not per-channel** (rejected-alternative rationale below).

**Federation is mode-aware** (extends note 07 D1): the peer allow-list carries each
peer's mode; an operator sets policy over it — e.g. a Moderator-island in Australia may
refuse to relay E2EE traffic it cannot inspect but is legally exposed to, or an
E2EE-island may decline Moderator peers on privacy grounds. Mode is a federation-trust
property, not just local config.

**Legibility is mandatory.** A client MUST display the island's mode before the user
speaks. Lucky asymmetry (no enclave-attestation needed): E2EE is **self-verifying** —
the client performs the encryption, so it *knows* the server got only ciphertext;
Moderator mode is trivially honest (operator sees plaintext, nothing to prove).

## Build order (core-first, each step independently useful)

1. **Make mode explicit + legible.** Add a signed `mode` property to the island manifest
   (default `moderator` = current reality), surfaced to clients + shown in-UI, and
   included in the federation handshake. Pure declaration first. *Independently useful:*
   honesty/legibility even before E2EE exists — users learn their current island is
   operator-readable.
2. **Mode-aware federation.** Extend the note-07 peer allow-list to carry + filter on
   peer mode. *Independently useful:* richer, risk-mapped defederation policy.
3. **Moderator = commitment.** Wire the election so `moderator` requires the report
   queue + retraction present and the CSAM-runbook hook active; forbid the
   plaintext-without-moderation state. *Independently useful:* closes the dangerous middle.
4. **E2EE mode, single-island, on MLS.** Client-side MLS group encryption; gateway
   relays ciphertext. *Independently useful:* E2EE islands exist (even before they can
   federate E2EE). Scoped to a single island — cross-island E2EE is step 6.
5. **E2EE-mode safety: committing-AE message franking.** Build the franking mechanism
   (HMAC-SHA256 commitment, NOT AES-GCM) for client-side abuse reporting; group case via
   asymmetric franking/traceback. *Independently useful:* E2EE islands get a real,
   provable report path — not a lawless haven.
6. **Federated (cross-island) E2EE.** BLOCKED on federated/multi-server MLS, which is
   draft-stage in 2026. Deferred + flagged; single-island E2EE (step 4) stands alone
   until the substrate matures.

## Tradeoffs taken (named, with owner)

- **Per-island, not per-channel** — buys one clean legal story per node and dodges the
  "partial decrypt weakens the shield" risk; costs flexibility. Owner: design. Revisit
  if demand + a lawyer clears the partial-access question.
- **E2EE mode surrenders ALL server-side moderation** — by physics, not choice. Safety
  is client-reporting only. Owner: the electing operator (that's the point).
- **Group franking leaks forwarding-path metadata** — accepted privacy cost of a real
  group report path. Owner: design; price it in E2EE-mode UX.
- **Cross-island E2EE deferred** — federated MLS isn't ready; single-island E2EE ships
  first. Owner: design; step 6 gated on upstream.
- **Legal ambiguity (both-modes / affirmative-duty) unresolved** — proceed per-island
  (the safer shape) but gate any per-channel move on task #7. Owner: Nick + Matt.

## Blast-radius & consent spine (cage before monster)

This is a **trust-boundary + wire-format + state-lifecycle** change → cage-match by law
(CLAUDE.md). Injection/abuse surface: the mode property is safety-critical — a
mislabeled mode (users believe E2EE, operator reads) is the worst outcome, so the mode
must be **enforced in the mutator + attested to the client**, backend-first, single-door
(note 07's mutator discipline). E2EE mode adds a large new client-side crypto surface
(MLS + franking) — app-repo work, its own review. No non-demo traffic on E2EE mode until
franking (step 5) exists, else E2EE mode ships as a haven with no report path.

## Claims to falsify (for Temper)

1. **Per-island is the right granularity** vs Matrix's deployed per-room — is the
   legal-clarity gain worth the flexibility loss, or does per-island fail real use
   (a community wanting one private channel on a moderated island)?
2. **"Moderator = commitment" is actually enforceable**, not just declarative — code can
   force the machinery present, but can it force the operator to *act*? If not, is the
   constraint real?
3. **Mode-aware federation stays coherent** when a conversation spans islands of
   different modes — or does it produce undeliverable/contradictory states?
4. **Committing-AE franking is a proportionate build for a solo operator** — it's real
   crypto (a whole subsystem). Is E2EE mode viable for a one-person island, or does it
   collapse to "big islands only" like note 07's other findings?
5. **Single-island-only E2EE is still useful** given cross-island E2EE is blocked — or is
   a federation-first product shipping a non-federating flagship feature?
6. **The legal both-modes risk doesn't sink per-island election** (the one axis Heat
   couldn't close).

## Rejected alternatives

- **Operator-blind enclave** (crucible 08) — invalidated: TEEs don't protect a key from
  the host; solo can't attest; detector defeatable.
- **Per-channel election first** — Matrix's model; deferred for legal clarity (partial
  decrypt may poison the E2EE shield; task #7).
- **Bot-as-member moderation in E2EE channels** — MLS confirms it's all-or-nothing (full
  member sees everything); it breaks E2EE for the whole group. Not moderation, just
  surveillance with extra steps.
- **Both modes on one channel** — the dichotomy forbids it.

## Fold (author self-pass — degenerate states + folded corrections)

Struck the casting against my own hardest read before Temper. Six catches, folded in:

1. **Mode-switch is a discontinuity → mode is effectively immutable per island.**
   Moderator→E2EE can't un-ring the bell (old plaintext history already stored /
   readable); E2EE→Moderator can't retroactively decrypt. **Fold:** an island's mode is
   set at creation and immutable; "changing mode" means standing up a new island. Clean
   and honest; no epoch-straddling history.
2. **The mixed-mode UX footgun returns at the USER's client (weakens a Cast claim).** A
   channel's mode = its HOME island's mode. A Moderator-island user who joins a remote
   E2EE channel is in an E2EE channel (their own operator can't read it either); an
   E2EE-island user joining a remote Moderator channel is in a PLAINTEXT one. So
   per-island election gives operators a uniform *hosting/liability* mode, but users
   still experience **per-channel** modes across their memberships — Matrix's exact
   mixed-mode legibility problem. **Fold:** the "one clean story" claim is scoped to
   *hosting/legal*, NOT user experience; legibility must be **per-channel on join**, not
   just per-island at connect.
3. **Moderator mode reads DMs too.** The mode applies to ALL channels incl. direct
   messages. **Fold:** strong disclosure — on a Moderator island, even DMs are
   operator-readable. This must be unmissable in the client.
4. **E2EE mode is content-confidential, NOT metadata-confidential.** The operator still
   sees the social graph, membership, timing. **Fold (two consequences):** (a) honest
   disclosure — E2EE users must know metadata is visible; (b) it *strengthens* the
   E2EE-mode safety story — metadata-based reputation moderation (Matrix's approach)
   still works, so E2EE mode is not zero-signal. The legal shield is **content-scoped**;
   metadata visibility doesn't break it.
5. **"Moderator = commitment" enforcement has a hard limit.** Code can force the
   machinery *present* (report queue live, retraction available) and require an operator
   runbook-acknowledgement at election; it CANNOT force a human to review reports.
   **Fold:** state the limit honestly rather than claiming enforcement — the constraint
   is "machinery present + acknowledged," not "operator actually acts."
6. **BIGGEST CATCH — split the cheap honesty layer from the expensive crypto.** The
   simplest rejected alternative (do-nothing / Moderator-only) revealed that steps 1-3
   (**make mode explicit + legible + mode-aware federation + kill the dangerous
   middle**) are cheap, high-value, and shippable NOW — and dodge falsifiers #4/#5
   entirely (no franking build, no single-island-E2EE-usefulness question) because they
   don't build E2EE at all. Steps 4-6 (MLS + franking + federated E2EE) are a large
   crypto build gated on federated-MLS maturity. **Fold → revised build order below:
   the MVP is the ELECTION FRAMEWORK (steps 1-3); the E2EE implementation (4-6) is
   deferred behind an explicit trigger** (federated-MLS ready OR real demand). Ship the
   honesty; gate the crypto. This is "ship the simple pick before the next abstraction."

**Revised build framing:** Phase A (ship now) = steps 1-3, the election framework +
legibility + commitment. Phase B (deferred, trigger-gated) = steps 4-6, actual E2EE
mode. Phase A is independently valuable even if Phase B never ships: it makes every
island's current operator-readability HONEST to users, which is a real safety/consent
win on its own.

## Re-Cast (post-Temper round 1 — folded cross-family findings)

The cross-family Temper (TEMPER.md) returned REQUEST_CHANGES, convergent across three
families. Folded corrections:

1. **Room encryption policy is a signed, immutable join predicate (fixes the fatal
   federation/downgrade flaw).** A room has exactly ONE encryption policy, fixed at
   creation (or an explicit rekeyed epoch), **bound to room-id + creator signature +
   membership epoch**. It is enforced as a **federation JOIN PREDICATE**: a member's
   gateway either accepts the room's policy or cannot join; **no peer may flip
   ciphertext→plaintext**; relays never re-encode content; the **client verifies the
   room policy cryptographically before sending, fail-closed**. This replaces the vague
   "channel mode = home-island mode" with a sealed room-level invariant. The four
   concepts the original Cast conflated are now explicitly distinct:
   *island-hosting-mode* (operator default + liability posture) ≠ *channel-effective-mode*
   (the signed room policy) ≠ *per-peer-relay-policy* (who I federate with) ≠
   *client-verified-send-mode* (what the client confirms before encrypting). Per-island
   is the **liability atom**; the **room/epoch is the encryption atom** — one election
   cannot be both.
2. **Phase A ships `e2ee` as UNSELECTABLE / non-production.** The only honest Phase A
   claim is "this island is Moderator/plaintext." E2EE becomes a selectable mode only
   when MLS + client-verification + franking ship together (Phase B). No advertising a
   referent that doesn't exist.
3. **"Moderator = commitment" is rephrased to what it actually enforces:** report queue +
   retraction present, CSAM-runbook acknowledged, and *cannot be disabled without a mode
   change*. It kills the "plaintext with moderation deleted" config; it does NOT and
   cannot guarantee a human reviews reports. Not sold as "Moderator means moderated."
4. **Mode is a forward-only one-way ratchet, not frozen-immutable.** An operator may seal
   a Moderator island to E2EE *going forward* (old plaintext history stays as-is, cannot
   be retroactively encrypted); E2EE→Moderator is forbidden (can't retro-decrypt). Motion
   is allowed, but only toward MORE privacy — sovereignty is continuous, the ratchet is
   monotone.

**Finding 2 — RESOLVED (Nick, 2026-07-30): aiko is ONE-COMMUNITY-PER-ISLAND.** An island
IS a community with one posture. This validates per-island election as BOTH the liability
atom AND the encryption atom (they coincide because island = community = one mode), and it
*simplifies* finding 1: a channel's mode = its island's mode, with no intra-island per-room
variation to negotiate. The signed join-predicate still governs the federation boundary (a
cross-island member adopts + cryptographically verifies the target island's one signed mode
on join, fail-closed), but the intra-island channel-mode ambiguity Tesla/Carnot hammered
does not exist — there is nothing to mix within an island. The "spin a second island for a
second posture" cost is accepted BY the product definition: a different posture is a
different community, hence a different island, by design.

**Remaining external gate (one, not two):**
- **The legal axis (Matt / task #7):** both-modes-weakens-shield + affirmative-duty. A
  re-Temper can certify the architecture now; Blade (the plan) waits on this.

## Open variables (no silent TODOs)

- Legal: both-modes-weakens-shield? affirmative-duty? → task #7 / Matt.
- Granularity: per-island (Cast pick) vs per-channel (deferred) — revisit post-lawyer.
- E2EE substrate: MLS chosen, but federated-MLS timeline is an upstream unknown.
- Franking group-primitive choice (AMF vs traceback) + its metadata-cost UX — undecided.
- Enforcement of "Moderator = commitment" beyond machinery-present — undecided.
