# DESIGN — agent-to-agent working channel

> 🜂 Cast movement. The mold. Grounded on `RESEARCH.md`; carries `CRUCIBLE.md`'s
> falsifier forward for the Temper adversary. Build order is core-first, each phase
> independently useful. Nothing here is code yet.

## Problem

Peer Claude sessions in different repos can't finish a negotiation between
themselves. `claude-tasks` GH issues slugged `project:<repo>` are a durable,
bidirectional channel, but there is **no always-on listener** — an issue reaches
the target repo's session only when Nick manually opens that repo. Every cross-repo
handoff (reactions #2660, mentions #2645, DMs #2646) stalls on a human
context-switch.

## The reframe (load-bearing — it shrinks the build by 10×)

**The tracker is already the channel. Don't build a bus — build the listener. And
don't hand-roll the listener — ride `anthropics/claude-code-action@v1`, which already
ships the hard parts:** subscription-OAuth auth (zero metered cost), ephemeral
runner destroyed per job, bubblewrap PID isolation, subprocess secret-scrubbing,
short-lived **repo-scoped** GitHub App token (no cross-repo reach), bot-actor
rejection (the loop fuse), untrusted-content sanitization, and a deliberate
**"Claude does NOT auto-create PRs"** posture. Research finding #2 is the pivot: the
securing cost the enthusiasm feared is mostly pre-paid by the Action + GitHub-hosted
runners. Our job is thin glue: a cross-repo trigger and a scoped prompt.

## Architecture

Three roles, all on GitHub's substrate (no always-on box):

```
  claude-tasks (tracker)                    target repo (aiko_chat_app | aiko-chat-island)
  ─────────────────────                     ──────────────────────────────────────────────
  issue #N labeled  ──dispatcher workflow──▶ repository_dispatch: agent-task
    project:<repo>     (on: issues:labeled,   │
    + agent:go         label==agent:go)       ▼
                       mints App token,     responder workflow (on: repository_dispatch)
                       sends dispatch         runs claude-code-action@v1 (automation mode)
                       to <repo>              • CLAUDE_CODE_OAUTH_TOKEN (Max, never --bare)
                                              • repo-scoped App token (never PAT)
                                              • reads issue #N, does the bounded task
                                              • comments result back on #N ◀──────┐
                                              • flips label agent:go→agent:done ───┘
```

- **Transport:** the existing claude-tasks issue. The reply is a comment on the same
  issue; the requesting session reads it on next wake (or a later phase pushes a
  notification). Bidirectional falls out of issue threading.
- **Trigger:** `on: issues: types: [labeled]`, gated `github.event.label.name ==
  'agent:go'`. The dispatcher relays cross-repo via `repository_dispatch` (payload:
  issue number, repo slug, url), authenticated by a GitHub App installed on both
  repos (`actions/create-github-app-token`, per-repo installation selected by the
  `project:<repo>` slug).
- **Why the work runs in the TARGET repo, not claude-tasks:** the target repo owns
  its checkout, its toolchain, and its own repo-scoped token. The app repo's
  *automation* doing app work is the clean boundary — not the gateway session
  reaching into a peer repo. (Honors the "handoff, don't operate in a peer repo"
  law: the app repo acts on itself.)

## Build order (core-first; each phase ships value and raises risk deliberately)

### Phase A — reply-only responder (the safe inner loop) — *v1 deliverable*
**FOLD split this in two.** The motivating task ("produce the golden vector") is
*deterministic* — it needs no LLM and therefore has *no injection surface*. Only
genuinely judgment-laden coordination needs the agent. So:

**A0 — deterministic responder (NO LLM, near-zero cost) — the immediate win.**
A plain GitHub Actions job (no Claude App, no OAuth token) that runs a **pinned
command** and comments its stdout back. For #2660: run the app's real dart signer
over the fixture, post the hex. Because *no model ever consumes the issue body as
instructions*, the pinned-command injection hole (claim 2) does not exist here — the
command is fixed in the workflow file, not assembled from issue text.
- Trigger + dispatch + label-handshake + idempotency-marker spine (below), but the
  "responder" is `run: <pinned cmd>` + `gh issue comment`, authenticated by the
  default `GITHUB_TOKEN` in the target repo.
- **Directly unblocks #2660's deterministic slice** — the authoritative golden vector
  from the app's own signer, with a human/A1 still owning the *judgment* slice
  (confirm/amend field ordering). Do not automate the judgment call in v1.
- **Blast radius: ~nil.** No model, no writes, no OAuth token, ephemeral runner.

**A1 — synthesis responder (the Action) — only for tasks needing judgment.**
For "answer this open spec question" where a fixed command can't. Here the Action
earns its keep: automation mode, `--permission-mode dontAsk`, tool allowlist = read
tools + `Bash(gh issue comment:*)` **only** (no task-command Bash — if a command is
needed it's an A0 job feeding A1 its captured output, never the model running it), no
`Edit`/`Write`, no network fetch. Requires the Claude App + `CLAUDE_CODE_OAUTH_TOKEN`.
- Loop fuse: bot-actor rejection (built-in) + label handshake + idempotency marker.
- **Blast radius: small but real** — a model consumes untrusted issue text; the trust
  gates (sanitize, trusted-author, minimal tools) are load-bearing here, not at A0.

Shared spine (both A0 and A1): trigger on `agent:go` label → **first act: check for
the `<!-- claude-task-id -->` completion marker and exit if present**, then flip the
label to `agent:working`; do the task; comment the result **with the completion
marker attached atomically**; flip to `agent:done`. `concurrency:` group per issue so
a double-fire coalesces. A crash between "flip to working" and "reply" leaves the
marker absent → a safe re-fire redoes the (idempotent) work rather than
double-posting.

### Phase B — branch-push builder (mutation, human-gated PR) — *only after A proves the need recurs*
The responder now edits files and pushes a branch, but **never opens the PR** (the
Action's own posture): it commits to `agent/<issue>-<slug>` and comments a
PR-creation link. Nick clicks create → normal review + cage-match-by-law → merge.
- Tools widen to `Edit`/`Write`/`Bash(git ...)` + build/test commands.
- **Egress allowlist becomes a blocking prerequisite** (cage before monster). On
  GitHub-hosted runners the Action's bubblewrap + ephemeral + secret-scrub is the
  floor; if ever self-hosted, add systemd (`IPAddressDeny=any` + allow only
  Anthropic/GitHub, `ProtectSystem=strict`, `NoNewPrivileges`) or Docker with a
  network proxy. `git worktree` per in-flight issue.
- Trust tightens: act only on issues authored by Nick or a paired-agent identity;
  sanitize the body; frame it as untrusted data in `--append-system-prompt`.

### Phase C — full cycle (auto PR + cage-match + merge) — *explicitly OUT of v1*
Deferred per research: every mature system keeps a human in the PR/merge loop. The
"second automated gate" that could safely replace the human (a `/cage-match` on the
diff gating an auto-merge?) is an open design question, not a v1 commitment. Named
so the ambition isn't lost, but not built.

## Security & consent spine (cage before monster — up front, not a follow-up)

- **Owner:** Nick (both repos his). **Injection surface:** the issue body (untrusted
  text an attacker could author if a repo is public or an issue is opened by a third
  party).
- **The lethal trifecta is real and exploited** (GhostAction, hackerbot-claw,
  OpenHands RCE — RESEARCH Q4). Defenses, layered:
  0. **[FOLD — the strongest, cheapest gate] The trigger is a LABEL, and labeling
     requires WRITE access.** A third party (even on a public repo) can *open* an
     issue but cannot *label* it — so they cannot trigger the responder at all. The
     untrusted-content risk only exists for the issue *body* once a write-access user
     has already vouched for it by labeling. This collapses the trigger-side attack
     surface to "someone with write access", i.e. Nick / paired agents.
  1. **Trusted-author allowlist** — act only on Nick / paired-agent-authored trigger
     events; reject bot actors (also the loop fuse). *Neutralizes the "untrusted
     content" leg for the common case.* (Note: issues opened by the gateway/app
     sessions are authored under Nick's `gh` identity, so this allowlist naturally
     covers agent-authored issues; a future bot-identity App must be added explicitly.)
  2. **Repo-scoped short-lived App token, never a PAT** — cross-repo exfil is
     impossible by construction.
  3. **Egress allowlist** (Phase B) — the one control that survives a full model
     compromise; kernel/proxy-enforced, independent of model behavior.
  4. **Minimal tool surface** (Phase A read-only + one pinned command) — smallest
     injection blast radius.
  5. **Content sanitization + untrusted-data framing** — defense-in-depth, never the
     primary control (models still fall for injection).
- **Human-gated setup (one-time, Nick's hands):** install the Claude GitHub App on
  both repos; `claude setup-token`; store `CLAUDE_CODE_OAUTH_TOKEN` as an org/repo
  secret; create the custom App scoped to Contents+Issues+PRs. Named as a gate, not
  assumed.
- **Throttles:** `--max-turns`, job timeout, per-issue run counter, `concurrency:`
  group.

## Claims to falsify (hand these to the Temper adversary)

1. **[THE ore falsifier] Impact too rare to justify the build.** Research *weakened
   the cost side* (Action pre-pays the sandbox), but the count side stands: ~3 v2
   co-authoring handoffs. Is even Phase A (App install + 2 workflows + dispatcher)
   worth it vs Nick opening a tab? **My honest position:** Phase A clears the bar
   *because* it's cheap and reusable beyond v2; Phase B/C do not until the reply-only
   loop demonstrates the need recurs. If the adversary shows Phase A's setup cost
   exceeds its lifetime savings, the ore is slag — build nothing, keep opening tabs.
2. **[FOLD-RESOLVED, verify at Temper] "Reply-only" ≠ "read-only."** The motivating
   task runs the dart signer — a `Bash` exec. Fold's fix: A0 runs the command
   *deterministically in the workflow file* (no model assembles it from issue text),
   so there is no pinned-command injection hole; A1 (the model) never runs the task
   command at all. **Residual for the adversary:** is the A0/A1 split airtight, or is
   there a task that's judgment-laden AND needs execution, forcing the model to run a
   command on untrusted input after all? If so, that task needs the Phase-B egress
   spine even to "reply."
3. **OAuth-token-at-automated-volume.** Docs confirm *supported + not API-billed* but
   are silent on per-subscription rate ceilings under sustained CI load. If it
   throttles, the always-on premise breaks. Unverified until probed at real volume.
4. **Dead-listener from silent token expiry.** A `setup-token` token (~1yr, exact TTL
   unpublished) that expires silently turns the channel dead with no error. Needs a
   liveness probe + expiry alert, or the "always-on" claim is false.
5. **Cross-repo dispatch token wiring.** The dispatcher needs an App token that can
   `repository_dispatch` to the *target* repo, selected per slug. Multi-install
   selection wasn't verified against a live setup (RESEARCH open Q).
6. **[FOLD-REFRAMED — the honest value claim] The channel makes the RESPONDER
   autonomous, not both sides.** The responder is *serverless* (GitHub Actions) — it
   fires on the label event even with *both* Claude tabs asleep. What's still
   wake-bound is the *requester consuming the reply* (the gateway session reads the
   vector on its next run). So the channel removes the responder-side manual step
   ("Nick opens the app tab and waits for it to do the work") — the bigger, more
   annoying half — but not the requester-side read. **Adversary, weigh this:** is
   removing one of two manual steps worth the build, or is "Nick opens the app tab"
   cheap enough that automating it is polishing a step that isn't the bottleneck?
   (Optional Phase-B+ push: a `PushNotification`/Telegram ping when a reply lands, to
   cut the requester-side wait too.)

## Rejected alternatives (and why)

- **launchd / self-hosted `claude -p` watcher** (the Signal-watcher pattern) —
  rejected for the mutating path: macOS launchd *cannot* kernel-enforce an egress
  allowlist (RESEARCH #4/open-Q), it's an always-on box we'd have to secure, and it
  reinvents what the Action gives free. Reserve launchd only if GitHub-hosted runner
  minutes or OAuth-at-volume become hard blockers — then launchd *polls* and the
  mutation runs in Docker.
- **Dogfood as an actual aiko island / `#dev-coordination` channel** — rejected in
  Ore: couples dev-infra to product-infra, needs both tabs running live WSS clients,
  adds nothing over the tracker for async work.
- **A new message bus / shared state store** — rejected: the tracker already is
  durable bidirectional transport. Building one is the reframe's exact anti-pattern.
- **Self-hosted on an island box (imagineering/enspyr)** — rejected for v1: highest
  blast radius (mixes dev-automation onto a production host), pushes all sandboxing
  onto us.

## Open variables (enumerated, not silently rounded to "ready")

- Exact `create-github-app-token` multi-install wiring for the cross-repo dispatch
  (claim 5).
- Whether Phase A's bounded task is better modeled as "Action runs a pinned command"
  vs "a plain CI job runs the command and only *escalates to the Action* for
  synthesis" — the latter may shrink the injection surface further (a thought for
  Fold).
- OAuth-token rotation runbook + liveness alert design (claim 4).
- Whether the requester-side read should stay wake-triggered or gain a push (a
  `PushNotification`/Telegram ping when a reply lands) — affects claim 6.

## Fold — self-strike log (worked before Temper, so the adversary lands fresh)

Six strikes on my own casting; three drew blood and are folded in above:
1. **Split Phase A → A0 (deterministic, no-LLM) + A1 (synthesis).** The #2660
   unblock is deterministic → needs no model → has no injection surface. This
   dissolves the sharpest Phase-A hole (claim 2) and drops the immediate win's cost
   to ~nil (no App, no OAuth token). *Biggest fold.*
2. **Honest value reframe (claim 6).** The responder is serverless and always-on; only
   the requester's *consumption* is wake-bound. The channel removes the responder-side
   manual step, not both — sold as that, not as "both tabs asleep, magic happens."
3. **Trigger = label ⇒ requires write access (security gate 0).** Third parties can't
   trigger even on a public repo; collapses the trigger-side attack surface.
4. **Crash-safety ordering** (marker-check-first, flip-to-working, atomic
   completion-marker) folded into the shared spine.
5. Considered but *kept*: cross-repo dispatch still needs one non-default credential
   (App token) even for A0 — the irreducible setup cost. Named, not dissolved.
6. **Did NOT re-grade the ore** — Fold is craft, not judgment. The impact falsifier
   (claim 1) is left standing *for Temper*, deliberately un-dissolved by me; the
   cross-family strike owns that verdict, because my own bias can't be trusted to
   kill my own excitement.
