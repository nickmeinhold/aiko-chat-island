# CRUCIBLE — agent-to-agent working channel

> 🜂 The enthusiasm case + the falsifier. Written at the consent gate (Nick
> pre-selected the candidate and chose crucible-first). Hot-phase artifact —
> handed to the Temper adversary alongside RESEARCH.md and DESIGN.md so it can
> catch what the excitement smuggled in.

## The ore

Peer Claude sessions across repos cannot finish a negotiation between themselves.
The **aiko-chat-island** gateway tab and the **aiko_chat_app** app tab coordinate
through `claude-tasks` GH issues slugged `project:<repo>`, restored on wake — a
durable, bidirectional channel. But the channel has **no always-on listener**: an
issue reaches the app tab only when Nick manually opens that repo. Every cross-repo
handoff stalls on a human context-switch.

**Motivating need (live, this session):** the signed reaction-bytes co-authoring
(#2660) is blocked waiting for the app tab to produce a golden vector from its real
Ed25519 signer. Same wall ahead for #2645 (mentions) and #2646 (DMs). I watched
this fail in real time — I had to ask Nick to go open the app tab.

## Why it thrills me (the heat, stated plainly)

Two Claudes finishing a spec negotiation between themselves, while Nick makes
coffee, is the *oh, of course*. It removes a recurring human-relay task, not just
adds an AI-mediated one (design-for-subtraction). And it's **reusable
infrastructure** — every future cross-repo seam (not just v2 social) rides it. The
ambition Nick chose to temper toward: a **full-cycle builder** — the responder
reads a slugged issue, builds the feature in the target repo, opens a PR,
cage-matches, merges, and replies, autonomously.

## The spark (one line that, if true, makes me want to drop everything)

> The tracker is already the channel; we are three hundred lines of launchd +
> `claude -p` away from the app tab answering #2660 by itself.

## The falsifier (the ONE thing that, if true, proves this ore is slag)

**The coordination need is too rare to justify an always-on autonomous mutator.**
Across all of v2 there are ~3 co-authoring handoffs, each of which Nick unblocks in
the ten seconds it takes to open a tab. A full-cycle builder that must be
OS-sandboxed, egress-filtered, and worktree-isolated to safely mutate a
**peer-owned** repo may cost far more to secure and maintain than it ever saves —
mechanism mistaking its own existence for impact
(`feedback_aliveness_fixation_impact_blindness`). **Temper must prove the impact
axis is independently real, not retrofitted to a cool mechanism.** If it can't,
report the candidate invalidated — an honest negative result.

## Scoring (aliveness × impact)

- **aliveness 3** — evidence: I hit the wall live this session (#2660 stalled on a
  manual tab-open); reusable across every v2 seam. I'd drop other work for it.
- **impact 2 (contested — this is exactly what Temper must adjudicate)** — evidence
  *for*: removes a recurring relay task, unblocks stuck co-authoring. Evidence
  *against*: the falsifier above (rare need, high securing cost). Scored 2 not 3
  precisely because the impact is the thing under dispute.
- Product = 6 (contested). Proceeding to forge because Nick pre-selected; Temper
  owns the impact verdict.

## Scope boundaries (fixed at the gate — Fold may not re-open these)

- **Reply-only responder is the safe inner loop; full-cycle builder is a superset
  of it.** Start the design from reply-only, escalate to full-cycle with the
  isolation spine in place.
- **Don't build a new bus.** The tracker IS the transport. The thing being built is
  the *listener*, not the channel.
- **REJECTED alternative (elegant-but-wrong):** dogfood the coordination channel as
  an actual aiko island / `#dev-coordination` channel. Couples dev-infra to
  product-infra, needs both tabs running WSS clients, adds nothing over the tracker
  for async co-authoring. Fun someday; wrong first move.
- **Trust model:** triggered on Nick's own repos + a specific label today (blast
  radius = Nick's own infra, not attacker-controlled). Must not silently accept
  untrusted input — the day it does, it graduates to the full isolation spine.

## Open variables (for Cast to enumerate, Temper to strike)

1. **Where the always-on listener runs** — Mac launchd (reuse the Signal-watcher
   pattern) vs GitHub Actions on claude-tasks vs an island-box daemon.
2. **Autonomy ceiling** — reply-only (bounded synthesis, comment back) vs
   full-cycle (mutate peer repo, PR, merge). The isolation requirements differ by
   an order of magnitude.
3. **Isolation spine for the mutating case** — sandbox user, egress filter,
   worktree isolation, and what confines the spawned `claude -p`.
4. **Loop-safety** — an agent that comments on issues that trigger agents can
   self-trigger. Needs a fuse.
5. **Auth/secret handling** — the OAuth token + `gh` creds the responder needs, and
   how they're scoped so a prompt-injected agent can't exfiltrate them.
