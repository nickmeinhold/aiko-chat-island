# Expand/Contract Migration Safety + CI Gate — DESIGN (Cast)

**Task:** #11 · **Unblocks:** #10 (reactive island deploy, PARKED) · **Status:** Cast
(design), pre-Temper. NOT built. This is a `/crucible` design pass, scoped to
Cast → Fold → Temper (Ore skipped — candidate already selected; heavy Heat skipped
— the reactive-deploy crucible already surfaced the crux).

---

## 1. Why we are here

The reactive-deploy crucible (`docs/crucible/reactive-deploy/`) converged on a
CONVERGENT FATAL: **image-only rollback cannot undo a database migration.** Re-pin
the old image and the old code meets a schema that was already migrated forward →
crash-loop or silent corruption. Tesla's line: *"backup without restore-on-failure
is a souvenir, not a spine."*

Nick re-picked the ROOT fix over the workaround: make every migration
**backward-compatible (N-1)** so the previous code runs safely against the new
schema. That makes image-only rollback genuinely safe — and buys zero-downtime
deploys and safe rollback generally, as a byproduct. Reactive deploy (#10) then
unblocks for free.

## 2. Grounding (live facts verified this session, not memory)

- **The image migrates the persistent volume at boot, fail-closed.**
  `entrypoint.sh` runs `python -m aiko_gateway.migrate` (→ `alembic upgrade head`,
  stamp-or-adopt) and only `exec uvicorn` on exit 0. So a deploy ALWAYS ratchets
  the schema forward before serving. This is the FATAL made concrete.
- **We never `alembic downgrade` in prod.** Rollback is image re-pin. The schema
  ratchets forward and stays. Therefore **safety does NOT depend on working
  `downgrade()` functions or on restoring the DB backup** — it depends only on the
  old code tolerating the new schema. This is the single most important framing
  correction and it simplifies everything downstream.
- **Tests are NOT shipped in the image.** The Dockerfile copies `src/`, `alembic/`,
  `entrypoint.sh`; hatch packages only `src/aiko_gateway`. So the naïve
  "run the previous image's test suite against the new schema" is impossible — the
  previous image has no suite. The runtime backstop must be reframed (§5.2).
- **Contracting ops in this repo are ALL `batch_op.*`, not `op.*`** (SQLite needs
  `batch_alter_table` table-recreate for most ALTERs). e.g.
  `batch_op.drop_column("community_id")`, `batch_op.drop_constraint(...)`. A lint
  keying on `op.drop_column` would wave every one of them through. The lint MUST
  descend into `with op.batch_alter_table(...) as batch_op:` blocks.
- Single replica, enforced by `worker_guard` flock. Migrations are documented as
  NOT concurrency-safe — one migrator at a time. (Bounds the design: we never race
  migrators.)
- Image tags: `:edge` tracks main; `vX.Y.Z` semver tags are the release anchors;
  every build also gets `:sha-<short>`. Last release `v0.3.0`. GHCR holds every
  versioned image (anon-pullable) — the rollback target is always retrievable.

## 3. The safety invariant (stated precisely)

> **N-1 read/write compatibility.** For any migration M that takes the schema from
> revision R to R+1, the code of the release that shipped schema R must continue to
> function correctly against schema R+1 — every read it issues resolves, and every
> write it issues succeeds and satisfies all constraints.

If every adjacent migration is N-1 compatible, then by induction any deployed image
version tolerates the schema left by the *next* version — which is exactly the
state an image-rollback lands in. That is the whole safety property; the CI gate
exists to enforce it.

Note the invariant is about **adjacent** versions. Islands can pin any version, but
a rollback only ever steps back to the immediately-prior deployed version, so
adjacent compatibility is sufficient and much cheaper to enforce than all-pairs.

## 4. The discipline (parallel change / expand-contract)

Every schema change is classified additive or contracting.

- **Additive changes ship in one release.** Add nullable column; add column with a
  `server_default`; create table; create index; relax a constraint (drop CHECK /
  UNIQUE / FK); widen a type.
- **Contracting changes split across THREE releases** (the Fold corrected this from
  two — see FOLD.md F1; two releases makes the *next rollback* unsafe):
  - **vB — Expand:** add the new shape alongside the old. If data must move, the new
    code dual-writes (writes both old and new) and reads prefer new, fall back to
    old. The migration is purely additive. **The old shape is still used.**
  - **vC — Stop-use:** code reads/writes ONLY the new shape. The old shape is still
    present in the schema but no longer touched by any code.
  - **vD — Contract:** drop the old shape. Safe **only because the rollback target
    (vC) already stopped using it.** Rolling back vD→vC lands old code that doesn't
    reference the dropped shape.

The three-release rule exists because **rollback re-introduces the prior release's
code.** The invariant a contract migration must satisfy is not "old code is drained
from all islands" (it isn't — a rollback brings it back) but: *the immediately-prior
release already stopped using the dropped shape.*

Classic worked example — **rename `messages.origin` → `messages.provenance`:**
1. vB Expand: add `provenance` (nullable), backfill, code writes both, reads
   `provenance ?? origin`.
2. vC Stop-use: code reads/writes only `provenance`; `origin` untouched.
3. vD Contract: drop `origin`. A rollback vD→vC is safe — vC never read `origin`.

## 5. The CI gate — the fork, resolved

The task named the fork: **static-lint of migration ops** vs **runtime compat
test**. Grounding shows it is a *false binary* — they catch disjoint failure
classes at very different costs. The design is **layered**: static-lint as the fast
mandatory floor, runtime-compat as the deeper backstop.

### 5.1 Static-lint (Phase 1 — mandatory floor)

AST-parse each migration **new since the last release tag `vX.Y.Z`** (NOT the `main`
merge-base — `ci.yml` runs on direct pushes to main too, where there is no PR
merge-base; "N-1 relative to the last *released* schema" is the safety-correct
baseline, and the release tag is ground truth for it — Fold F2),
walk the `upgrade()` function ONLY (downgrades are never run in prod — §2), and
classify every Alembic op. Descend into `batch_alter_table` blocks and treat
`batch_op.*` identically to `op.*`.

**Contracting ops → FAIL the build** (unless escape-hatch annotated, §5.3):

| Op | Why it breaks N-1 |
|---|---|
| `drop_column` | old code SELECTs a column that's gone → error |
| `drop_table` | old code queries a table that's gone |
| `rename` (column/table) | old code references the old name |
| `add_column(nullable=False)` **without** `server_default` | old INSERT omits it → NOT NULL violation |
| `alter_column` → `nullable=False` **without** `server_default` | old rows / old inserts violate NOT NULL |
| `alter_column` type change | narrowing can truncate/reject old writes — flag ALL type changes for review |
| `create_*_constraint` (CHECK/UNIQUE tightening) | old code may write rows the new constraint rejects |

**Safe ops → pass:** `add_column` (nullable or with `server_default`),
`create_table`, `create_index`, `drop_constraint` (relaxing), type-widen.

Properties: deterministic, runs in the existing `ci.yml` in seconds, no infra. It
catches every *structural* contracting op — which is the bulk of real breakage.
**What it CANNOT catch: semantics.** A column kept but repurposed, a data migration
(`op.execute("UPDATE …")`) that changes meaning, a value-range assumption. That is
exactly the runtime backstop's job — clean division of labour.

Implementation candidates (Temper to weigh): hand-rolled `ast` walk (zero deps,
full control, must track the `batch_op` binding) vs `alembic`'s own op
introspection. Hand-rolled `ast` is likely simplest and dependency-free, matching
the CI's "stdlib-only, no aiko_services" isolation invariant.

### 5.2 Runtime-compat backstop (Phase 2 — reframed to git/image reality)

The gold standard is empirical: prove the old code runs against the new schema.
Reframed around §2's constraints (no tests in image; test fixtures build their own
DB and would migrate it to the *old* head, defeating the test):

**Use the rollback topology itself as the test.**
1. NEW image (this PR's build) migrates a **seed DB** to the new head.
2. PREVIOUS release image (`v0.3.0`, pulled from GHCR) serves that migrated DB with
   **migration bypassed** — a new `entrypoint` env flag `MIGRATE=skip` (or a
   `command:` override) so the old image doesn't try to run migrations its alembic
   doesn't know about.
3. Smoke the old container: `/health` + a scripted **read/write** probe over the
   core endpoints (post a message, read history, a moderation read). Pass = the old
   code tolerates the new schema empirically — the exact rollback scenario.

This needs exactly one new capability — a `MIGRATE=skip` entrypoint branch — and
reuses artifacts we already publish (versioned GHCR images). It is heavier
(pull + boot two containers) so it runs as a separate job, gated to run only when
`alembic/versions/**` changed.

The "run the full previous suite" variant is rejected: tests aren't in the image,
and fixtures own their own DB — it would require invasive test-harness surgery for
strictly less realism than the serve-the-migrated-DB smoke.

### 5.3 The genuinely-destructive escape hatch

Sometimes you MUST contract (the second half of an expand/contract, or a real
drop). The gate must ALLOW it *auditable*, never silently bypassable. A migration
may carry a machine-readable annotation the lint recognizes:

```python
# expand-contract: contract-phase
# stop-use-shipped-in: v0.4.0   # the release whose code STOPPED using the dropped shape
# rollback-target: v0.4.0       # the immediately-prior release a rollback would land
# rationale: origin superseded by provenance; v0.4.0 reads/writes only provenance
```

**Honest framing (Fold F1): this is a human ATTESTATION, not a lint-checked
invariant.** The safety condition for a contract migration is "the immediately-prior
release (the rollback target) already stopped using the dropped shape" — a property
of *code*, which the AST lint fundamentally cannot verify. So the lint's role here is
narrow: it confirms the annotation is *present and well-formed* (a `contract-phase`
tag with a `stop-use-shipped-in` that is **strictly older than the current release**,
and a `rollback-target` matching it), downgrades the hard FAIL to a WARN, and forces
the PR to carry a `migration-contract` label so a human signs the attestation. No
annotation → hard FAIL. `stop-use-shipped-in` == current, or missing → hard FAIL. The
gate makes the escape *explicit, dated, and reviewer-signed* — it does not and cannot
*prove* rollback safety; that proof lives in the Phase-2 runtime smoke (run the
`rollback-target` image against the contracted schema) and in the human's attestation.

## 6. What this design deliberately does NOT do

- **Does not require working `downgrade()`.** Prod never downgrades; the schema
  ratchets forward. Downgrades stay for dev convenience but are not the safety
  spine and are not gate-checked.
- **Does not make the DB backup load-bearing** for the common case. With N-1
  compatibility, rollback needs no DB restore. The `update.sh` backup becomes
  belt-and-suspenders for the rare annotated contract-phase migration.
- **Does not touch the single-replica / migrate-at-boot topology.** Orthogonal.

## 7. Build plan (phased)

- **Phase 1 — static-lint + discipline** (unblocks #10 with a named residual):
  `ci.yml` job running the lint on changed migrations; the annotation escape hatch;
  a `docs/` note documenting the expand/contract discipline; unit tests for the lint
  (feed it known additive + contracting migrations, assert pass/fail).
- **Phase 2 — runtime-compat smoke** (closes the semantic-drift residual):
  `MIGRATE=skip` entrypoint branch; the seed-migrate-serve-smoke job; wire it to run
  on `alembic/versions/**` changes.

## 8. Open questions for Temper

1. **Is Phase 1 alone sufficient to unblock reactive deploy (#10), or is Phase 2 a
   hard prerequisite?** Reactive deploy is *unwatched*; the reactive-deploy FATAL
   named "healthy-but-wrong release" as the true danger of unwatched deploy, and
   semantic drift (which only Phase 2 catches) is a healthy-but-wrong vector. My
   position: Phase 1 unblocks *watched/manual* rollback safety; **Phase 2 is a hard
   prereq before UNWATCHED reactive deploy.** Pressure this.
2. **Baseline for the lint diff.** "New migrations vs `main` merge-base" enforces
   adjacent-compat by induction. Is there a gap when several unreleased migrations
   stack on `edge` between two release tags? (I believe induction covers it, but a
   family should try to break it.)
3. **`add_column` with `server_default` — is it truly always safe?** Old code
   reading rows gets the default; old INSERTs omit it → default applies. Writes to
   the new column? Old code doesn't know it exists, so it can't. Believed safe;
   verify no path where a DB-side default disagrees with an app-level invariant.
4. **`drop_constraint` classified safe (relaxing) — counterexample?** Dropping a
   UNIQUE lets duplicates in that old code's read path may assume unique
   (`.one()` → MultipleResultsFound). Should UNIQUE-drop be WARN, not silent-pass?
5. **Does the lint's AST walk correctly bind `batch_op`** when the context manager
   uses a non-standard `as` name, nested batches, or `op.execute` raw SQL that
   contains DDL? Raw-SQL DDL (`op.execute("ALTER TABLE …")`) is an AST blind spot —
   is a raw-`execute`-in-a-migration itself a WARN? **Deeper (Fold F3):**
   `batch_alter_table` RECREATES the table in SQLite and can express a contracting
   change through its *arguments* (`copy_from`, `reflect_args`, a redefined column
   set) rather than an explicit `batch_op.drop_column()` call — an op-walk misses it.
   Is a batch block with recreate-config args itself a WARN-for-review?
6. **Phase-2 old-image false-fail (Fold F4).** The `MIGRATE=skip` smoke assumes the
   old image does no schema assertion past the migrate module. But `migrate.py` runs
   a fail-closed stamp-or-adopt ORM-vs-DB diff. If any startup path beyond migrate
   re-checks `alembic_version`/the ORM diff, the old image will refuse the new-head
   DB and the smoke false-fails. Verify before building Phase 2.

---

*Next: Fold (author self-strike) is in this same file's sibling FOLD.md, then
cross-family Temper via `/cage-match` on this design.*
