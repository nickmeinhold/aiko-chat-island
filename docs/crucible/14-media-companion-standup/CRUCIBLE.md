# CRUCIBLE — Media companion standup (LiveKit SFU + embedded STUN/TURN)

**Task:** #14 (TURN parity + Caddy-managed cert auto-renewal, both islands)
**Seed / lean:** companion standup sibling to the island, *not* a member of the island's pinned image.
**Forged:** 2026-08-11

## The pick

Bring the self-hosted **LiveKit SFU** — which carries STUN/TURN *inside it* (pion/turn), so there is no separate coturn to add — under the island's management discipline: **version-pinned image, repo-authoritative compose, Caddy-managed auto-renewing TURN cert, one-command standup + pull-based update, forced-relay acceptance test.** Land it as a **companion sibling** to the island unit, not folded into the island's single pinned image.

## Why this thrills me AND what it changes

- The drift the island model was *built to kill* is sitting one directory over, un-managed. Both boxes run `livekit/livekit-server:latest` (unpinned). imagineering's TURN cert died **Jul 24** (18 days ago, static Apr-25 cert, no renewal). enspyr has **no `turn:` block at all**. This is not hypothetical rot — it's live breakage.
- **What it removes:** relay-fallback video is currently broken on imagineering and absent on enspyr. Users behind symmetric NAT / restrictive corporate firewalls **cannot connect** right now. This is a real failure class, not a convenience.
- **What it unblocks:** "a fresh island can do video out of the box" — media becomes part of the standup story, so federation grows a media plane instead of a hand-built snowflake per box.
- The *oh, of course*: point the island's own proven discipline (pin → deploy-from-repo → Caddy cert → verify) at the thing next to it. The mechanism already exists; it just isn't aimed here yet.

## The one-line spark

> The island already knows how to kill this exact drift — we just never pointed it at the media plane.

## Claims to falsify (what the temper must strike)

1. **FRAME — is the media plane island-shaped at all?** My lean says "companion to the island." The strongest counter is *not* "merge it into the island image" (I have good reasons against that) — it's **"it's box-level shared infra, not island-adjacent."** Ground truth: imagineering's LiveKit serves `realm-token.imagineering.cc` (token mint), dreamfinder, lyra, tech-world, AITW (Firebase webhook + Redis dispatch). That is a *box service the island consumes*, not an island appendage. If true, "companion standup" is the wrong home on imagineering and the design must model **consume-vs-own** explicitly, not assume symmetry.
2. **ASYMMETRY is load-bearing.** enspyr's LiveKit is bare + island-dedicated; imagineering's is multi-tenant. A single companion-standup that treats both boxes identically will either break imagineering's other tenants (if it re-templates their shared config) or under-deliver on enspyr. The design must survive *both* shapes with one mechanism, or explicitly fork.
3. **Cert delivery crosses a container boundary asymmetrically.** imagineering runs Caddy in a **container** (cert store in a Docker volume); enspyr runs Caddy as a **systemd service** (cert store on the host FS). "Caddy-managed" is true on both, but the renewal→deliver→restart hook is *not* the same mechanism. If the design assumes one delivery path, it's wrong on one box.
4. **pion/turn does not hot-reload its cert** (to verify in RESEARCH) — so cert renewal *requires a livekit restart*, which is a media-service interruption. On imagineering that restart drops dreamfinder/lyra/AITW calls too (blast radius beyond the island). The renewal hook's restart is not free.
5. **Don't add coturn.** We only use LiveKit for media; embedded TURN is already wired. Standalone coturn is more moving parts for zero gain (design-for-subtraction). If the temper argues for coturn, it must show a media use-case LiveKit's embedded TURN can't serve.

## Rejected alternatives (carried for the adversary)

- **Fold LiveKit into the island's pinned image.** Rejected: couples two planes with different release cadences + blast radii; forces imagineering's *shared* SFU into an island-exclusive role it doesn't have; a UDP-port-range host-networked media server doesn't belong in the gateway's compose.
- **Do nothing / keep hand-managing.** Rejected: that's the status quo that produced an 18-day-dead cert and a TURN-less second island.
- **Standalone coturn instead of embedded TURN.** Rejected: subtraction — see claim 5.

## Impact & aliveness (rubric)

- **aliveness 3** — evidence: an 18-day-dead cert + a TURN-less island + `:latest` on both boxes; the exact drift class the island exists to kill, un-managed, one dir over.
- **impact 3** — evidence: relay-fallback video is *currently broken* for NAT/firewall-restricted users on one island and absent on the other; fixing it restores a connectivity class + gives every future island a media plane.
