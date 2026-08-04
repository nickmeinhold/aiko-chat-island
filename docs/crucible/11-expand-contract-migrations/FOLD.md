# Fold — author's adversarial self-strike on DESIGN.md

The author (Claude, same instance that Cast the design) striking its own design
before cross-family Temper. Weight: near-zero on intent-vs-bytes (Temper's job),
full weight on domain-local correctness the cross-family adversary is blind to.

## F1 — FATAL in the escape hatch: rollback re-introduces the retired code (self-catch)

The §5.3 escape hatch permits a contract-phase (destructive) migration when
`expand-shipped-in` is *older than the current release*. **This is wrong, and it is
wrong in exactly the way this whole project exists to prevent.**

The safety property is *rollback* safety. A contract migration in vN drops column X.
Rollback steps vN → vN-1. If vN-1 code still reads X, the rollback crashes — and my
guard permits `expand-shipped-in = vN-1`, where vN-1 is the *expand* release that
still dual-reads X. So the guard green-lights a migration that makes the very next
rollback unsafe.

The real lifecycle to drop X is **three releases, not two**:
1. **vB expand** — add Y, dual-write X+Y, read `Y ?? X`. X still used.
2. **vC stop-use** — code reads/writes only Y. X present but untouched by code.
3. **vD contract** — drop X. Safe *only because vC (the rollback target) stopped
   using X.*

The correct guard: a contract migration is rollback-safe iff **the immediately-prior
release already stopped using the dropped shape**. That is a *code* property the AST
lint fundamentally cannot verify — so the escape hatch is a **human attestation**,
not a checked invariant, and the design must say so honestly instead of dressing it
as a lint rule. My Cast collapsed three releases to two and overstated what the gate
verifies. → folding the correction into DESIGN §4 + §5.3.

## F2 — lint diff baseline is under-specified for push-to-main (self-catch)

§5.1 says "diff vs `main` merge-base." `ci.yml` runs on PR *and* push-to-main; on a
direct push there is no PR merge-base. The safety-correct baseline is "migrations
added since the last release tag `vX.Y.Z`" — that is what "N-1 relative to the last
*released* schema" actually means. Merge-base is a convenience proxy for the PR case;
the release tag is ground truth. → folding into DESIGN §5.1.

## F3 — batch_alter_table blind spot is deeper than documented (carry to Temper)

§2 notes the lint must descend into `batch_op.*`. But `op.batch_alter_table(...)` in
SQLite RECREATES the table, and the recreate can express a contracting change through
its *arguments* (`copy_from`, `reflect_args`, a redefined column set, `recreate=...`)
rather than through an explicit `batch_op.drop_column()` call. An AST op-walk that
only classifies method calls on the batch binding can miss a column dropped via
recreate configuration. I do not have a clean answer; this is real ammunition for the
Temper rather than something I can close now. Leaving as sharpened DESIGN Q5.

## F4 — Phase-2 backstop: does the OLD image do its own schema validation? (carry)

The `MIGRATE=skip` smoke assumes the old image, once past migrate, does no further
schema assertion. But `migrate.py` has a fail-closed stamp-or-adopt path that runs
alembic's metadata comparison. If ANY startup path beyond the migrate module
re-checks `alembic_version` or the ORM-vs-DB diff, the old image will refuse the
new-head DB and the smoke will false-fail. Must verify before building Phase 2.
Leaving as sharpened DESIGN Q (added).

## F5 — additive-only actually buys MORE than the §3 "adjacent" framing admits

Minor, but worth stating precisely so Temper doesn't waste a round on it: if EVERY
migration is additive, any older code tolerates any forward schema (cumulative adds),
not just adjacent — so the property is stronger than "adjacent-compat." The escape
hatch (F1) is the ONLY thing that breaks the all-forward property and reintroduces an
adjacency constraint. This means: **islands that never use the contract escape hatch
can roll back across arbitrarily many versions; islands that do are pinned to
adjacent-only rollback across that boundary.** That is a real operational property
worth documenting for #10.

## Disposition

F1 and F2 are clear corrections → folding into DESIGN now (strengthen before Temper,
so the families strike the corrected form, not a hole I already know about). F3, F4,
F5 are carried into DESIGN as sharpened open-questions / properties for the Temper.
The Fold did NOT find a frame-level FATAL — but the reactive-deploy Fold didn't
either, and the Temper found the FATAL there. That precedent is exactly why this
design does not ship on the Fold's say-so.
