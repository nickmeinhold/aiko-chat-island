# Expand/Contract Migration Safety — DESIGN v2 (Re-Cast)

**Task:** #11 · **Unblocks:** #10 · **Supersedes:** `DESIGN.md` (v1), which the
cross-family Temper (`TEMPER.md`) hit with a CONVERGENT FATAL (4/4). This v2 answers
that Temper. **Status:** Cast (v2), pre-Fold / pre-re-Temper. NOT built.

Read `TEMPER.md` first — this doc is structured as its point-by-point resolution.

---

## 0. What changed since v1

- **Part B (the boot-half) SHIPPED** — PR#116 (`c03df0e`), 5-way cage-matched. The
  migrate module is now **forward-revision tolerant**: a DB stamped at a revision
  unknown to the image is served, not crash-looped, under the alertable marker
  `MIGRATE_SKIP_UNKNOWN_REVISION`. So Tesla's required invariant **(B)** — "lastgood
  boot/migrate treats DB-ahead as success without mutating" — is DONE. This v2 is
  about the rest.
- The FATAL is resolved at the boot layer. What remains is making the **served
  schema actually tolerable** (part A) and being honest about scope.

## 1. The invariant, restated as the Temper demanded

Image rollback is safe iff the CONJUNCTION holds:

- **(A) schema tolerance** — the lastgood *code* reads/writes the post-deploy schema
  correctly. ← this doc (the lint discipline + runtime backstop).
- **(B) boot tolerance** — the lastgood *boot/migrate* treats a DB at an
  unknown/future revision as success without mutating. ← SHIPPED (PR#116).

v1's error was proving (A)'s structural half and calling image rollback "safe." (A)
is necessary; **(A) ∧ (B)** is the property. And even (A) ∧ (B) is only the **DB
half** of *island* rollback — see §6.

## 2. Multi-hop rollback: the invariant is cumulative, not adjacent (Temper #2)

v1 §3 claimed "adjacent compatibility is sufficient." The Temper broke this: reactive
deploy + GHCR tags let an island jump `v0.3.0 → v0.5.0` in one pin change, and
`:lastgood` is the *previous successful digest*, NOT `semver−1`. So a rollback target
can be **N-k releases back**, across contract boundaries. The true property (Tesla):

> `code(image_lastgood)` must tolerate `schema(head_after_the_failed_deploy)`

which is **cumulative N-k compatibility**, not adjacent N-1. It holds automatically
for purely additive migrations (any older code tolerates any forward *additive*
schema — v1's F5), and is **broken only by a contract-phase migration** between the
rollback target and head. This reframes the whole gate: **the lint's job is to keep
the migration stream additive so cumulative compatibility is free; the ONLY thing
that can break it is a contract, which is exactly what the escape hatch (§5) governs
and the rollback actuator (§4) must respect.**

## 3. Part A — the static lint, taxonomy HARDENED (Temper #3)

Same shape as v1 §5.1 (AST-walk `upgrade()` only, descend into `batch_op.*`, diff
since the last release tag), but every false-safe row the Temper found is corrected.

**Contracting → FAIL** (unless escape-hatch annotated, §5):

| Op | Why | New in v2? |
|---|---|---|
| `drop_column` / `drop_table` | old code reads it | v1 |
| `rename` (incl. `alter_column(new_column_name=…)`) | old code uses old name | **hardened** — Wu: rename hides as an `alter_column` kwarg, the most ordinary op |
| `add_column(nullable=False)` / `alter_column`→NOT NULL, **no** `server_default` | old INSERT violates NOT NULL | v1 |
| ANY `alter_column` type change | narrowing truncates; on SQLite even a "widen" rides a table-recreate that changes affinity/rounding | **hardened** — Wu: kill the v1 "type-widen is safe" row; ALL type changes are reviewed |
| `create_*_constraint` (CHECK/UNIQUE tighten) | old writes rejected | v1 |
| **`create_index(unique=True)` / `add_column(unique=True)`** | a unique index IS a constraint tighten → old writes hit `IntegrityError` | **NEW** — Wu; v1 passed `create_index` unconditionally |
| **`drop_constraint` (UNIQUE / FK / CHECK)** | UNIQUE-drop → old `.one()` → `MultipleResultsFound`; FK-drop → silently disables `ON DELETE CASCADE` → orphans; CHECK-drop → out-of-range values old readers assume impossible | **NEW** — v1 wrongly marked all `drop_constraint` "safe (relaxing)"; the whole row leaves silent-pass |
| **`batch_alter_table` recreate-via-arguments** (`copy_from`, reduced reflected column set, `recreate=`) and **trigger/dependent loss** on recreate | contracting change with no explicit `drop_*` op; recreate silently drops triggers | **NEW** — Wu/Tesla F3; this repo centers batch rebuilds, so it's the main path |
| **raw `op.execute("…")`** | AST can't classify SQL-in-a-string | **NEW** — the lint only FLAGS "raw SQL present, semantics unverifiable statically"; it does NOT adjudicate it (Fold F2.3 — a backfill `UPDATE` and a semantic re-key `UPDATE` are indistinguishable to the lint). The **§4 runtime smoke is the real gate** for raw-SQL migrations |

**Still safe → pass:** `add_column` (nullable, or `server_default` on a
non-security/authz/billing column — see below), `create_table`, plain
`create_index(unique=False)`.

**`add_column` + `server_default` is only STRUCTURALLY safe (Temper #3, Tesla).**
Old INSERTs get the DB default — fine structurally. But a `NOT NULL role DEFAULT
'user'` means an old admin-provisioning path that omits `role` writes rows the new
code reads as non-admin → healthy-but-wrong, green gate. So: `server_default` on a
column the lint can't prove is semantically inert stays **review-tagged**, not
silent-pass. (The lint can't know "semantic"; the pragmatic rule is review-tag
`server_default` + `NOT NULL` together, pass `server_default` + nullable.)

**What the lint STILL cannot catch: semantics.** A column kept but repurposed, a data
migration that changes meaning. That is the runtime backstop's job (§4) — the
division of labour v1 got right.

## 4. Part A — the runtime backstop, now on the REAL boot path (Temper #1, resolved by PR#116)

v1's Phase-2 invented `MIGRATE=skip` to route the smoke *around* the boot fatal — the
Temper's sharpest catch (it tests a topology production never runs → false-pass).
**PR#116 dissolves this**: the production migrate module is now forward-tolerant, so
the smoke can boot the **real** lastgood image the **real** way. No bypass flag.

**The smoke = the production rollback topology, exactly:**
1. NEW image (this PR's build) migrates a seed DB to the new head.
2. The **actual `:lastgood` image** (pulled from GHCR — NOT a declared
   `rollback-target` that can disagree with `:lastgood`, Tesla) boots against that
   migrated DB **through its normal `entrypoint.sh`** — which now hits
   `MIGRATE_SKIP_UNKNOWN_REVISION` and serves, as production would.
3. Probe the invariants migrations actually touch — post a message, read history, a
   moderation read, an auth round-trip — NOT just `/health` (Kelvin/Tesla: `/health`
   is liveness, not readiness; healthy-but-wrong is the unwatched-deploy killer).

This catches the semantic drift the lint can't, on the exact path prod runs.

**Bootstrapping precondition (Fold F2.1):** the smoke boots the old image *relying on
forward-tolerance*, which only exists as of PR#116. `:lastgood` on the live islands is
still v0.3.0 and would crash-loop, not skip. So the smoke is valid only once its "old"
image is ≥ the first *released* forward-tolerant version — i.e. **#116 must be cut as a
release**, and that release is the floor for any image the smoke boots as "old." Until
then A2 cannot run against a real old image.

**Gate the smoke by asserting `MIGRATE_SKIP_UNKNOWN_REVISION` fired** — otherwise the
old image might have *upgraded* the seed DB (if the seed wasn't actually ahead), and
the smoke would prove nothing. Bind the test to the marker PR#116 added.

## 5. The escape hatch — attestation made CI-checkable (Temper #5)

v1's hatch checked `stop-use-shipped-in` is "older than the current release." Wu/Tesla
broke this: the lint runs on migrations *since the last release tag* — i.e. **before
the current release is cut**, so "current release" is a version that doesn't exist yet
and the check degrades to guesswork ("unreleased trunk collapse": expand+stop-use+
contract all land between tags; a human mis-sets `stop-use-shipped-in` to the current
window; gate WARNs and passes; rollback still SELECTs a dropped column).

**v2 fix — bind the attestation to a REAL, ALREADY-PUBLISHED tag, and let the runtime
smoke verify it:**
- The annotation names `stop-use-shipped-in: vX.Y.Z` where **`vX.Y.Z` must already
  exist as a git tag** (the lint checks `git rev-parse vX.Y.Z` resolves) — not a
  future/guessed version. A contract migration therefore cannot ship in the same
  release train as its own stop-use; the stop-use must be a *published* release.
- The §4 runtime smoke then boots **that exact `stop-use-shipped-in` image** against
  the contracted schema and runs the read/write probes. Green = the attested
  stop-use release genuinely doesn't touch the dropped shape — the attestation is
  *verified*, not just *asserted*. Red = the contract is blocked.
- Human gate (`migration-contract` label) still required — but it now signs a
  machine-verified claim, not an unfalsifiable one.

This is the honest version of v1 §5.3's "attestation, not invariant": the invariant is
now checkable because the stop-use release is a real artifact we can boot.

## 6. Scope honesty — DB half only (Temper #4)

v1 §1 said this "unblocks reactive deploy for free." Overclaim. The N-1 invariant is
**DB-local**; the island is not. A migration can change registrar identity, MQTT topic
naming, HyperSpace addressing, signed-envelope fields, or the identity/recovery
protocol — all N-1 at SQLite, un-rollback-able at the bus/protocol layer.

**v2 states the boundary:** #11 delivers **DB-schema** rollback safety. Bus/HyperSpace/
protocol rollback compatibility is a SEPARATE concern and an explicit **residual on
#10** (reactive deploy must not treat "DB N-1 green" as "island rollback safe"). §1's
"for free" is retracted.

## 7. Rollback-actuator ↔ contract-window coupling (Temper #2, for #10)

The three-release discipline is a *build-side* human process; the *runtime* rollback
path (#10's `:lastgood` re-pin) can still violate it — `:lastgood` is a digest, knows
nothing about contract boundaries, and can roll back across one. v2 defines the
**contract #10 must consume** (implementation is #10's, but #11 owns the contract):

- The **author** records the contract boundary in a repo-tracked manifest
  (`deploy/contract-boundaries.json`: revision → `stop-use-shipped-in` tag) as part of
  the contract-migration PR, and the **lint VERIFIES** it (Fold F2.2 — a CI job pushing
  back to the repo is awkward and race-prone; author-writes / lint-checks mirrors the
  annotation posture). The lint FAILs if a contract op has no matching manifest row, or
  a manifest row cites a tag that doesn't resolve. This manifest is the SoT for "which
  version pairs are NOT rollback-safe."
- #10's actuator, before an unwatched auto-rollback, consults the manifest: if the
  rollback would cross a contract boundary (target older than a contract's stop-use
  tag), it **refuses the unwatched rollback and escalates to a human** (or falls back
  to DB restore for that boundary). Watched/manual rollback across a contract is the
  operator's informed call.

This is the coupling v1 lacked: the discipline is enforced at the runtime edge, not
just trusted on the build side.

## 8. Build plan (v2)

- **A1 — hardened static lint + manifest** (`ci.yml` job + `deploy/contract-boundaries.json`
  writer; unit tests feeding known additive/contracting/batch-recreate/raw-SQL
  migrations). Unblocks *watched* rollback safety.
- **A2 — real-path runtime smoke** (boot actual `:lastgood` / attested stop-use image
  through its own entrypoint against the migrated seed DB; assert
  `MIGRATE_SKIP_UNKNOWN_REVISION`; probe read/write invariants). Closes semantic drift;
  **hard prereq for UNWATCHED #10** (Temper Q1, unanimous).
- **A3 — actuator coupling** (owned by #10, contract from §7).

## 9. Open questions for re-Temper

1. **AST vs alembic-op introspection for the lint** — with the batch-recreate-via-args
   and raw-SQL cases now in scope, is a pure `ast` walk still tractable, or does the
   lint need to actually import + introspect the migration's ops? Tradeoff: `ast` stays
   in the CI's stdlib-only isolation; introspection is more accurate but imports the
   migration (and its deps).
2. **Does the §4 smoke's seed DB need representative DATA**, not just schema? A semantic
   drift (repurposed column) only bites rows that exist — an empty seed may pass a
   probe that a populated one fails. How much seed data is enough without becoming a
   fixture-maintenance burden?
3. **§7 manifest staleness** — is a repo-tracked JSON the right SoT, or should the
   contract boundary live in the migration file itself (annotation) and be *compiled*
   to the manifest by the lint (single source, §a la producer-roster discipline)?
4. **A1-alone shipping** — A1 makes *watched* rollback safe and produces the manifest.
   Is shipping A1 before A2 sound (watched deploy only), or does that re-create v1's
   "structural-only, semantics unproven" gap under a new name?
5. **`server_default` review-tagging heuristic** — is "NOT NULL + server_default →
   review, nullable + server_default → pass" the right line, or too permissive (Tesla's
   `role='user'` case was NOT NULL, but a nullable semantic column could still bite)?

---

*Next: Fold (author self-strike on v2), then cross-family re-Temper. Per CLAUDE.md:
this is a DESIGN temper — a green re-Temper leaves the IMPLEMENTATION unproven and
still owes a code `/cage-match` on each built diff (A1, A2).*
