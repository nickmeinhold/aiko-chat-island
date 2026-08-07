# TEMPER — agent-to-agent working channel

> 🜂 The cold pole. Cross-family cage-match on the DESIGN (not code). **Verdict:
> CANDIDATE INVALIDATED — do not build.** An honest negative result: the enthusiasm
> was real and load-bearing (it scouted + cast a genuine design), and the fire
> disposed of it in the same pass, before a line of implementation code. The engine
> worked.

## Panel

Four adversaries fired; **three seated** (gate = Maxwell + ≥2 adversaries → met).
Wu (Kimi K3) dark-seated this round (1-byte output, a silent-K3 miss). The three
that seated are three distinct model families — Gemini, GPT, Grok — and they were
**unanimous**.

| Reviewer | Family | Verdict |
|---|---|---|
| Kelvin | Gemini 3 Pro | **CANDIDATE INVALIDATED** |
| Carnot | GPT/Codex | **CANDIDATE INVALIDATED** |
| Tesla | xAI Grok | **CANDIDATE INVALIDATED** |
| Wu | Kimi K3 | dark-seated (no output) |
| Maxwell | Claude (me) | **CANDIDATE INVALIDATED — concur** |

## Why the ore was slag (convergent findings)

The three families independently struck the falsifier `CRUCIBLE.md` pre-registered
(claim 1) and **confirmed it**:

1. **Impact too rare — the falsifier held.** ~3 near-term handoffs × ~10s manual
   unblock each. Even the cheap A0 phase costs a custom GitHub App install, two
   workflows, cross-repo `repository_dispatch` wiring, a label handshake,
   idempotency, and forever-maintenance. Carnot: *"not a Carnot engine; a turbine
   attached to a doorbell."* Textbook `aliveness-fixation → impact-blindness`.
2. **The value is half a loop, and the flagship example proves it.** The responder
   becomes serverless/autonomous, but the requester still consumes the reply on its
   next wake — one of two manual steps removed. Worse: A0's golden vector for #2660
   *still needs a human to confirm/amend the field ordering*, so the flagship task
   **re-inserts the very human relay the channel claimed to delete** (Tesla).
3. **The A0/A1 split is a per-task workaround, not a reusable architecture
   boundary.** The moment a task is judgment-laden AND needs execution (mentions
   #2645, DMs #2646, any "run X, interpret Y"), the boundary collapses and pulls
   Phase-B isolation into "reply-only." A0 is really a *"workflow-edit factory"* —
   every new deterministic task needs a pinned command committed to the target repo,
   so "reusable without human authorship" is fiction.
4. **"Reusable infrastructure" is a mortgage on a future that may not arrive** — the
   backlog evidence is three handoffs, and the reuse is asserted, not demonstrated.
5. **The "near-zero cost" A0 smuggles an unverified cost:** cross-repo dispatch via a
   multi-install App token — the linchpin of the whole architecture — is never
   live-proven. If it's harder than assumed, A0 isn't cheap; it's a credentialing
   spike before the first useful byte.
6. **The "always-on" claim rests on unclosed risks:** OAuth-subscription behavior at
   CI volume + silent token expiry. A dead listener is *worse* than none — it
   manufactures a false expectation of progress.
7. **The simpler alternative that DISSOLVES the problem** (missing from my Rejected
   list, all three flagged it): for #2660, a `workflow_dispatch` / path-triggered CI
   job *in the app repo* that runs the dart signer and posts the hex (or commit the
   golden-vector fixture once). No dispatcher, no `agent:go`, no multi-install App.
   For the other two handoffs: **open the tab.** Tesla's rule: *"Build the third time
   it hurts twice in a week — not a bus for a bus that already exists without a
   passenger schedule."*
8. **Tesla's unique catch (integrity, not exfil):** A1's plausible-but-wrong
   synthesis becoming a protocol/crypto source-of-truth for the requester is
   **pollution** — lethal for co-authoring golden vectors even with comment-only
   tools. The security spine defended exfiltration, under-weighted pollution.

## What the panel credited (kept for the record)

- **Tracker-as-transport / no new bus** — correct subtraction of scope.
- **Riding `claude-code-action`** instead of a hand-rolled launchd watcher —
  research-backed pivot; pre-pays sandbox, loop fuse, scrubbing, no-auto-PR.
- **A0 no-LLM fold** — genuinely dissolves the sharpest injection hole.
- **Label-as-write-access gate** — a real primary trigger defense.
- **The Fold log's honesty** — claim-6 reframe + leaving claim 1 standing for Temper.

## Maxwell's concurrence (owning the call, not just relaying)

I concur, and it stings in the right way: the A0/A1 split I was proud of is the
thing that broke cleanest — it's a task-specific workaround wearing an
architecture's clothes, and my own flagship (#2660) demonstrates the loop doesn't
close. My Fold correctly *weakened the cost side* (the Action pre-pays the sandbox),
but three families correctly kept the *count side* standing: three handoffs do not
amortize a new moving part, and the reuse is speculation. **This is a clean, honest
CANDIDATE INVALIDATED. Do not build the channel.** Blade (plan mode) is not entered
— there is no surviving design to plan.

## Salvage (the useful thing the fire left)

- **#2660 (reaction golden vector):** if the manual tab-open is genuinely annoying,
  a *single* one-shot CI job in `aiko_chat_app` (`workflow_dispatch` → run the dart
  signer → comment the hex, or commit the fixture once) delivers the whole win
  without any channel. Otherwise: open the app tab once. Either beats the channel.
- **The trigger to revisit:** when a *third* cross-repo co-authoring genuinely hurts
  inside a short window — not before. "Build the third time it hurts twice in a week."
- **Lesson worth keeping:** a mechanism existing (branch, Action, dispatcher) is not
  impact; verify the impact axis is *independently* real before committing. The
  crucible caught exactly the trap it was built to catch.
