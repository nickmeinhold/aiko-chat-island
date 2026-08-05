# Re-Temper — cross-family cage-match on DESIGN-v2.md

Five families (Maxwell author-side; Kelvin/Gemini, Carnot/Codex, Tesla/Grok, Wu/Kimi).
Raw: `/tmp/temper2-{kelvin,carnot,tesla,wu}.md` (session-transient).

## Verdict: NOT SOUND, but SPINE HOLDS — tighten before build, do NOT re-cast

Kelvin FATAL, Carnot FATAL, Tesla REQUEST-CHANGES, Wu REQUEST-CHANGES. The two FATALs
and the two REQUEST-CHANGES share ONE objection (below); Tesla's disposition — echoed
in substance by all four — is *"do not re-cast the whole spine; tighten the claims and
fill specific gaps, then it goes DESIGN-SOUND."* Every family listed the v2 invariant
reframe, taxonomy hardening, tag-bound escape hatch, and scope honesty as genuine
advances over v1. This is an over-claim + gap-fill pass, not a re-pick.

## THE convergent objection — the THIRD boundary: (image × schema) but NOT (× data × paths)

v1's fatal was the wrong *boot* boundary; v2 fixed it and then laundered a THIRD
boundary the same way. Schema tolerance is not `(old image) × (forward schema)` — it is
`(old image) × (forward schema) × (post-migration DATA) × (exercised PATHS)`. The §4/§5
smoke proves only: *this image booted, the skip marker fired, and a FIXED probe suite
passed against ONE seed.* It does NOT prove semantic drift is closed. Same over-claim
SHAPE as v1's `MIGRATE=skip` — the bypass is now thin-seed + closed-world probes.

Concrete failure (Tesla): a raw-SQL migration re-keys `status` `0/1 → 'active'/'inactive'`.
Empty/int-only seed + probes that write/read via the old image → green. Production volume
already holds string statuses written by new code → rollback serves, real traffic 500s or
mis-branches. The lint correctly refuses to adjudicate raw SQL (F2.3) and hands it to the
smoke — but the smoke can't see it either.

Four sharpened forms:
- **Kelvin:** the smoke never tests *old code reading data WRITTEN BY THE NEW code* —
  which is the actual rollback scenario (new image runs, writes new-shaped rows, THEN
  re-pin). The new image only ever *migrates* in the smoke; it never *serves*.
- **Wu:** probe coverage is a fixed list with NO coupling to the migration under test →
  a contract against an un-probed table passes green BY CONSTRUCTION. And §5's "genuinely
  doesn't touch the dropped shape" is false: lazy loads, error handlers, admin/cron/
  recovery paths outside the four probes still SELECT the dropped column.
- **Carnot:** you cannot prove the full production state space in CI; the smoke is one
  synthetic trajectory. A contract verified only by booting the stop-use image proves the
  binary doesn't touch the shape *on the seeded paths*, not that older production data is
  legible.
- **Tesla:** "state topology ≠ boot topology." §9 Q2 (seed data) is not polish — it is
  load-bearing for every "verified" / "closes semantic drift" sentence.

**Fix (all four agree):** phrase claims at PROVEN scope — "attestation SAMPLED against a
DECLARED probe surface," not "verified." Require each migration PR to NAME the invariants/
paths it touches and BIND probes to that set (fail "coverage undeclared"). Seed with
production-representative rows INCLUDING a pre-swap phase where the NEW image writes rows
the old image then reads. Accept that semantic N-k safety is a CONFIDENCE probe, not a
proof — so unwatched deploy across a non-additive migration must not be greenlit by it.

## Second convergent finding (Wu + Tesla): §7 coupling is UNCOMPUTABLE + unbound

Wu, using v2's own §2 as the proof: §7's gate is "target OLDER THAN a contract's stop-use
TAG," but §2 established `:lastgood` is a DIGEST with no release ordering. No digest→tag→
index mapping exists in v2; nothing computes "older than"; the manifest (`revision → tag`)
lacks the info. v2 diagnosed v1's "documented-but-unenforced constraint is no constraint"
and then shipped exactly that, "one layer better dressed." Plus:
- **Runtime transport unspecified (Tesla):** WHICH manifest bytes at decision time? The
  failed image's bundled copy predates any contract merged after it shipped — the boundary
  row that should block the rollback is invisible precisely in the artifact deciding.
- **No append-only enforcement (Wu):** the lint verifies new contract ops have rows; a
  later PR can delete a row and no check fires, silently reopening a closed boundary.
- **Watched path uninformed (Wu):** nothing surfaces the manifest to the operator mid-
  re-pin, so "watched = operator's informed call" describes an operator never informed.

**Fix:** make the gate computable via REVISION ordering (linear, unlike semver) — bake the
image's alembic head revision as an OCI label at build; the actuator reads the lastgood
image's revision label and checks the manifest for any contract boundary between it and
head. Specify a decision-time SoT (read the contract map from the currently-deployed/failed
artifact or an append-only remote log stamped at migrate-success; fail-closed if unreadable;
NEVER from lastgood's older copy). Enforce manifest append-only.

## §3 taxonomy — remaining gaps

- **`server_default` + NULLABLE is ALSO false-safe (Wu + Tesla):** the default applies when
  the column is OMITTED, regardless of nullability. v2's "nullable → pass" is wrong. Fix:
  ANY `server_default` the lint can't prove inert → review-tag (allowlist-of-inert, or a
  denylist name match for role/authz/billing/permission).
- **No new-table carve-out → false-UNSAFE on the commonest shape (Wu):** `create_index
  (unique=True)` / `add_column(NOT NULL)` / CHECK on a table `create_table`'d in the SAME
  diff are safe (no old code ever wrote it), but v2's rows FAIL them → the flagship
  hardening fires on routine "add a feature table" migrations → the exact WARN-fatigue/
  rubber-stamp F2.3 warns about. Fix: cross-op diff analysis — exempt ops on a same-diff
  created table.
- **`create_foreign_key` missing (Tesla):** new FK → old write inserts orphan → IntegrityError.
  Add to FAIL; also a unique-index drop via `drop_index` (not `drop_constraint`) may evade
  the UNIQUE-drop row depending on AST shape.
- **batch detection needs a false-positive budget (Tesla):** flag-every-batch → fatigue on
  this batch-centric repo; narrow → false-safe. Needs golden fixtures from THIS repo's real
  migrations (recreate vs true-additive batch) and a measured FP budget.

## Other findings

- **A1-before-A2 is not an enforced state (Tesla):** nothing in A1 prevents wiring auto-
  rollback, and a healthy-but-wrong (`role='user'`) passes a *watcher's* green health check
  too. Fix: the #10 unwatched flag must not EXIST in code until A2 ships; A1 release notes
  must not say "rollback safe" — only "structural risk reduction under a human."
- **§0 "DONE" leaks the overclaim (Wu):** (B) is DONE as an ARTIFACT but ABSENT from the
  fleet — `:lastgood` is still v0.3.0, which crash-loops. Distinguish shipped-artifact from
  live-on-fleet; §8's "A1 unblocks watched rollback safety" is inert until the fleet's
  lastgood is ≥ the first forward-tolerant release.
- **CI `:lastgood` ≠ island rollback digest (Tesla):** the smoke pulls the moving GHCR
  `:lastgood`, not the digest THIS island will re-pin; multi-island skew means the smoke
  never boots island B's actual target. Bind the smoke to the digest range the actuator may
  select, or state CI-lastgood is a proxy and island-local lastgood is a #10 residual.
- **Dual-SoT drift (Tesla, §9 Q3):** prefer COMPILE — annotation is SoT, lint EMITS the
  expected manifest in CI and fails if the committed JSON differs (author commits, CI never
  pushes). Single producer, verified artifact.
- **§6 honesty leak (Tesla):** §8 "Unblocks: #10" still reads island-ready; #10's acceptance
  criteria must name a non-DB (bus/MQTT/HyperSpace/envelope) residual checklist, not only a
  prose residual.

## What v2 got right (consensus)

(A) ∧ (B) invariant with (B) actually shipped; killed `MIGRATE=skip`; cumulative-not-
adjacent reframe with additive-free induction; taxonomy hardening (rename-as-kwarg, all type
changes, unique indexes, full `drop_constraint`, batch-recreate, raw-SQL honesty); escape
hatch bound to a PUBLISHED tag (ends unreleased-trunk guesswork); §6 DB-half scope retraction;
author-write/lint-verify manifest posture; A2-hard-prereq-for-unwatched preserved. Spine sound.

## The meta-pattern (the session's throughline — worth a memory)

Three adversarial rounds, one repeated move: a mechanism claiming to PROVE rollback safety
that actually only SAMPLES it (escape hatch → boot actuator → smoke-as-proof). The durable
lesson: **this problem resists a fully-automated "rollback is safe" green check. Semantic
safety is a sampling/confidence problem, not a decidable one.** The achievable, honest
guarantee is ADDITIVE-ONLY ENFORCEMENT (structural, lint-provable) + named residuals for
semantics/data/cross-version — never a proof. Every runtime-smoke claim must say "sampled";
unwatched deploy across a non-additive migration must stay conservative; semantic migrations
are watched-deploy-human-judgment.

## Disposition — v3 TIGHTEN pass (not a re-pick); OR shrink #11 to A1

Two honest paths for Nick:
- **v3 tighten:** the 5 edits above (scope §4/§5/§8 to "sampled"; §7 computable via revision
  labels + decision-time SoT + append-only; §3 nullable-default + FK + new-table carve-out;
  A1 structural-only with a hard technical unwatched-gate; CI-lastgood vs island-digest).
  Then re-Temper → expected DESIGN-SOUND. Spine unchanged.
- **Shrink #11 to A1 (the subtractive move the meta-pattern argues for):** ship ONLY the
  hardened additive-only lint + the manifest as the structural guarantee; drop the ambition
  that a CI smoke PROVES semantic safety; treat semantic migrations as watched-deploy-human-
  judgment, full stop. Makes #11 tractable and honest; A2 becomes an explicitly-"sampled"
  confidence probe, optional and non-gating.

STAMP: design-only verdict. Even a DESIGN-SOUND v3 leaves A1/A2 IMPLEMENTATION unproven and
owes a code `/cage-match` on each built diff.
