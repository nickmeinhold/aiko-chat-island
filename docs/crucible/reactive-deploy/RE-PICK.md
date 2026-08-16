# Re-pick — Nick's frame decision on reactive deploy

*Movement 5.5. The Temper (2026-08-01) returned **FATAL as cast** and explicitly
declined to re-cast, because the fatal finding challenged the frame **Nick** chose:
"per the crucible's honest-negative-result rule: STOP at Temper, report the fatal
finding + the reframe, hand the re-pick to Nick." This file records that re-pick.*

## The ruling (Nick, 2026-08-16)

> "I think each island operator should have the option to have the server
> automatically pull the latest image... it's what I want."

## What this changes about the Temper's verdict

**It does not touch the fatal finding.** Image-only rollback still ≠ restore of previous
serving state for a self-migrating, data-holding service. A bad migrating release still
leaves a box at forward-schema + old-binary. That is mechanism, not opinion, and no
framing dissolves it.

**But it does weaken the Temper's strongest NON-DB argument**, and this is the part the
crucible never modelled. Tesla and Kelvin both closed on: *"the human watching `/health`
after a manual deploy is CHEAP SAFETY that the unwatched frame throws away."* That
argument silently assumes **the operator is Nick** — technical, ssh-capable, watching.

Nick's re-pick introduces a party the crucible never considered: a **third-party island
operator**. For them the counterfactual to auto-pull is not "watched manual deploy" — it
is **"never updates."** The relevant comparison becomes:

| | operator = Nick | operator = third party |
|---|---|---|
| without auto-pull | watched manual deploy (safe) | **island rots on an old image, unpatched** |
| with auto-pull | human safety removed | update path exists at all |

So on the axis that matters most for a federated deployment — **security patching of
islands nobody is babysitting** — auto-pull is the *safer* option, not the riskier one.
The Temper was right about Nick's two boxes and silent about everybody else's.

This is the same shape as ADR-0004's lesson: a decision correct inside its frame,
reversed by a frame the author did not hold.

## Selected shape

**Crucible fork options 2 + 3, composed** — which is what the Temper itself flagged as
the principled path:

- **Option 3 (enabler): expand/contract migration discipline.** Every migration
  backward-compatible with N-1, enforced by CI. This is what makes image-only rollback
  *genuinely* safe, which is what dissolves the fatal finding rather than routing around
  it.
- **Option 2 (the capability): migration-gated auto-deploy.** Auto-pull applies to
  releases the lint proves additive-only; a release carrying a contracting migration
  does not auto-deploy and notifies instead.
- **Operator-scoped, opt-IN, per island** — an `.env` setting, defaulting OFF. This is
  the literal content of the ruling ("each island operator should have the **option**")
  and it matches how every other island behaviour is operator-governed.

## Open questions this re-pick does NOT settle

1. **"Latest image" = which channel?** The original cast chose the **release-tag**
   channel (`v*` semver), explicitly *not* `edge`. For third-party operators that is
   almost certainly still right — `edge` tracks `main` and would auto-ship unreleased
   work — but it is now a product decision, not just ours.
2. **Who is notified on a skipped/failed auto-deploy**, when the operator is not Nick
   and has no access to our channels?
3. **Does a third-party operator get auto-rollback at all**, or does gating to
   additive-only migrations make rollback unnecessary by construction? (If the lint
   guarantee holds, the latter — which is much simpler.)

## Blocking state (2026-08-16)

The enabler is **stalled**. PR#117 (`feat/migration-lint-a1`, the additive-only lint)
took **4/4 REQUEST_CHANGES** in cage-match on 2026-08-05 and has had no commits since.
The rework is well specified in claude-tasks #2615 — 10 items, mostly AST traversal
correctness and closing a costume escape-hatch.

**Nothing about the auto-pull capability can be safely built until that lint is
trustworthy**, because the lint IS the safety gate the whole shape rests on.

Tracked: #2457 (this capability), #2458/#11 (expand/contract), #2615 (the lint rework).
