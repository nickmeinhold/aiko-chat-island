# Temper — cross-family design cage-match on DESIGN.md

Five families struck the design (Maxwell/Claude author-side; Kelvin/Gemini,
Carnot/Codex-GPT, Tesla/Grok, Wu/Kimi as independent adversaries). All four
adversaries seated — a rare full 5-way. Raw reviews: `/tmp/temper-{kelvin,carnot,
tesla,wu}.md` (session-transient).

## Verdict: CONVERGENT FATAL (4/4 adversaries: FATAL)

Not slag — **fixable and in-family.** The spine (no `downgrade()`; schema ratchets
forward; app must tolerate the forward schema; layered lint+runtime gate; three-
release expand/stop-use/contract) is RIGHT. The design is INCOMPLETE in a way that
makes its central claim false as written.

## THE convergent fatal — the migrate-at-boot ACTUATOR, not the schema

Carnot, Tesla, and Wu **independently** struck the identical flaw; Kelvin's semantic
strike points adjacent. The design fixed the wrong boundary.

The real production rollback path is NOT "old app code meets new schema." It is "old
IMAGE BOOT meets new schema" — and `entrypoint.sh` ALWAYS runs `alembic upgrade
head` (fail-closed) before uvicorn. On a re-pin to `v0.3.0`, the old image's alembic
meets an `alembic_version` row stamped with a **future revision that does not exist
in its own script directory**. Alembic cannot resolve history from an unknown
current revision → it errors → the fail-closed entrypoint refuses to `exec uvicorn`
→ the rolled-back island **crash-loops at boot**. *Even with a perfectly GREEN gate
and 100% N-1-compatible schema, image rollback still fails* — not with corruption
this time, with a boot refusal.

The sibling crucible's fatal was "rollback meets forward-migrated **schema**." This
design's fatal is "rollback meets forward-migrated **`alembic_version` bookkeeping**"
— and it sits exactly in the component DESIGN §6 waved out of scope as
"orthogonal." Wu: *"It is not orthogonal; it is the kill shot."*

**The laundering (worst part):** DESIGN §5.2 step 2 invented `MIGRATE=skip`
specifically "so the old image doesn't try to run migrations its alembic doesn't
know about" — engineering the Phase-2 smoke *around* the fatal. So the smoke tests a
topology production never runs and would **false-PASS**. My Fold F4 even touched this
exact path but **inverted** the bug — I worried `MIGRATE=skip` would false-*fail*,
when the truth is it false-*passes* by suppressing the precise failure production
rollback will hit. Same frame-blindness as the reactive-deploy Fold, one layer down.

**The fix (Wu names it, in-family, small):** the migrate module must become
**forward-revision tolerant** — detect that the DB's current revision is unknown to
the local script directory ("DB is ahead of me"), treat that as a recognized state:
log loudly, SKIP `upgrade`, serve. Never stamp down; never fail closed on it. Then
Phase-2 tests the REAL entrypoint path (forward-tolerant migrate) against the actual
lastgood GHCR image — NOT a `MIGRATE=skip` CI-only bypass.

Tesla states the corrected invariant precisely — safety = the **conjunction** of:
- **(A)** lastgood *code* correctly reads/writes the new schema  ← what DESIGN designed
- **(B)** lastgood *boot/migrate* treats "volume revision ⊆ unknown/future" as
  success without mutating  ← what DESIGN MISSED; must be a first-class product change

## Other convergent findings

**2. Multi-version / non-adjacent rollback (4/4).** "Adjacent compatibility is
sufficient" (§3) is false: reactive deploy + GHCR tags let an island jump
v0.3.0→v0.5.0 in one pin change, and `:lastgood` is "previous successful digest,"
NOT "semver−1." Rollback target can be N-k back, across contract boundaries. The true
property is `code(image_lastgood) compat schema(head_after_failed_deploy)` — cumulative,
not adjacent. My F5 noticed the property but filed it as a doc note; for UNWATCHED
deploy a documented-but-unenforced constraint is no constraint (Wu). Needs a coupling
the design lacks: the rollback actuator must know the contract window, OR contract
migrations gate on observed island convergence, OR forbid multi-hop rollback across a
contract.

**3. Lint taxonomy has multiple FALSE-SAFE rows (4/4).** Correctable, but real:
- `drop_constraint` silent-pass → FALSE-SAFE. Drop UNIQUE → old `.one()` →
  `MultipleResultsFound`; drop FK → silently disables `ON DELETE CASCADE` → orphans,
  silent corruption; drop CHECK → out-of-range values old readers assume impossible.
  The whole row leaves silent-pass.
- `add_column` + `server_default` → FALSE-SAFE semantically. Tesla's example: NOT
  NULL `role` default `'user'`; old admin-provisioning INSERTs without `role` → admins
  stored as users → healthy-but-wrong, green gate. Security/authz/billing columns stay
  review-tagged.
- `create_index(unique=True)` / `add_column(unique=True)` → FALSE-SAFE (Wu). A unique
  index IS a constraint tighten; old writes hit `IntegrityError`. `create_index` is
  currently unconditional-pass.
- **rename via `batch_op.alter_column("origin", new_column_name="provenance")`** →
  FALSE-SAFE (Wu). A rename as a kwarg on the most ordinary op — my lint checks
  `alter_column` only for nullable/type. This is F3's blind spot in the *common* path,
  not the exotic `copy_from` one.
- `batch_alter_table` recreate-via-arguments (`copy_from`, reduced reflected column
  set, `recreate=`) drops columns/tightens types with no explicit op; also silently
  drops **triggers** and view/index dependents. This repo CENTERS batch rebuilds — the
  main path, not an edge case.
- type-widen internal contradiction: table says "flag ALL type changes"; safe-list
  passes "type-widen." On SQLite a widen rides a recreate that changes affinity/rounding
  of existing rows. One rule: "all type changes, reviewed."
- pure data migrations (`op.execute("UPDATE …")`) that re-key enums / change units /
  repurpose columns → FALSE-SAFE, invisible to Phase-1.

**4. DB-N-1 ≠ island rollback safety (Carnot, Tesla, Wu).** The invariant is
DB-local; the island isn't. A migration can change registrar identity, MQTT topic
naming, HyperSpace addressing, signed-envelope fields, identity/recovery protocol —
all N-1 at SQLite, un-rollback-able at the bus/protocol layer. §1's "unblocks reactive
deploy for free" OVERCLAIMS. #11 delivers the DB half; bus/protocol rollback is an
explicit residual on #10.

**5. Attestation "strictly older than current release" is unfalsifiable at CI time
(Wu, Tesla).** The lint runs on migrations "since the last release tag" — BEFORE the
current release is cut as a tag. "Current release" at lint time doesn't exist yet;
the check degrades to guesswork, and nothing verifies the stop-use release was ever
deployed. Tesla's "unreleased trunk collapse": expand+stop-use+contract all land
between v0.3.0 and the next tag; human sets `stop-use-shipped-in: v0.3.0` wrongly;
gate → WARN+label; rollback still SELECTs a dropped column.

**Q1 verdict (unanimous): NO.** Phase-1 lint alone must NOT unblock unwatched #10.
And Phase-2 as specified (`MIGRATE=skip`) is ALSO insufficient — it must exercise the
real production boot path (forward-tolerant migrate), run the actual lastgood image,
and probe the invariants migrations touch, not just `/health`.

## What holds (consensus)

- Root direction: no `downgrade()` spine; schema ratchets forward; app must tolerate
  forward schema. The sibling-crucible diagnosis is right.
- §2 grounding is the strongest part — survives every strike.
- Three-release expand/stop-use/contract (F1) is right; "immediately-prior release
  stopped using the dropped shape" is the correct contract-phase condition.
- F2 release-tag baseline over merge-base is right; additive-only induction over
  stacked `edge` migrations (Q2) genuinely holds — stop worrying about it.
- Layered lint+runtime architecture is sound; false-binary correctly rejected.
- Attestation-not-invariant honesty (§5.3) is the right epistemic posture.
- Taxonomy findings are correctable rows, not an indictment of the lint idea.

## Disposition — RE-CAST v2 required (not slag, not ship)

The fatal is fixable and the fix direction is unanimous. v2 must add, over v1:
1. **(B) forward-revision-tolerant boot migrator** — the load-bearing product change
   (touches the fail-closed boot path in `aiko_gateway.migrate`).
2. **Phase-2 tests the REAL path** — actual lastgood image + forward-tolerant migrate,
   not `MIGRATE=skip`; probe migration-touched invariants, not just `/health`.
3. **Rollback-actuator ↔ contract-window coupling** — define the contract #10's
   actuator consumes (or forbid multi-hop rollback across a contract).
4. **Taxonomy hardening** — every false-safe row above.
5. **Scope honesty** — #11 = DB half of rollback safety; §1 "unblocks for free"
   downgraded; bus/protocol rollback an explicit #10 residual.

Because v2 expands #11's scope into the fail-closed boot path (a trust/state-lifecycle
surface), the scope-expansion decision is Nick's. STAMP: this is a **design-only**
verdict — even a green v2 re-temper leaves the IMPLEMENTATION unproven and still owes
a code `/cage-match` on the built diff.
