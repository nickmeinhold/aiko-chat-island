# Fold v2 — author self-strike on DESIGN-v2.md

Author (Claude) striking its own v2 before the cross-family re-Temper. Full weight on
domain-local issues; near-zero on intent-vs-bytes (re-Temper's job).

## F2.1 — the runtime smoke has a BOOTSTRAPPING dependency I didn't state (self-catch)

§4/§5 boot "the actual `:lastgood` image through its own entrypoint," relying on
forward-tolerance so the old image serves the migrated DB. But forward-tolerance only
exists as of PR#116 — **`:lastgood` on the live islands is v0.3.0, which pre-dates it
and would crash-loop, not skip.** So the smoke can only use a **post-#116** image as
the "old" side. Concrete constraint: the §4/§5 smoke is valid only once the
`stop-use` / `:lastgood` image is ≥ the first *released* forward-tolerant version
(i.e. #116 must be not just merged but CUT AS A RELEASE, and that release is the
floor for any image the smoke boots as "old"). Until then A2 can't run against a real
old image. → folding this precondition into DESIGN-v2 §4 + build plan.

## F2.2 — the §7 manifest can't be CI-WRITTEN; make it author-written, lint-VERIFIED (self-catch)

§7 says "the lint records the contract boundary in the manifest." A CI job pushing
back to the repo is awkward (push perms, commit loops, races with the PR head). The
clean shape: the **author** edits `deploy/contract-boundaries.json` as part of the
contract-migration PR, and the **lint VERIFIES** the manifest entry matches the
migration's annotation (revision + `stop-use-shipped-in`) — fail if a contract op has
no matching manifest row, or a manifest row cites a non-existent tag. Author-writes,
lint-checks — same posture as the escape-hatch annotation, no CI write-back. → folding
into §7.

## F2.3 — the raw-`op.execute` rule (§3) is a false comfort; the smoke is its real gate (self-catch)

§3 lists raw `op.execute("…")` as WARN-for-review. But: a pure backfill
(`UPDATE … SET new = old`) is common and usually additive-safe, while a semantic
re-key (`UPDATE … SET status = 'x' WHERE …`) is exactly the dangerous case — and both
are `UPDATE`s. The lint cannot distinguish them from the SQL string, so a blanket WARN
is either noise (→ rubber-stamping) or false confidence. Honest position: the lint can
only *flag that raw SQL is present* and record "semantics unverifiable statically";
the **§4 runtime smoke is the actual gate** for raw-SQL migrations. Don't let §3 imply
the lint adjudicates them. → softening §3's raw-SQL row + pointing it at §4.

## F2.4 — "additive ⇒ cumulative-safe is free" (§2) is true STRUCTURALLY, not SEMANTICALLY (carry)

§2's clean induction ("additive → any older code tolerates any forward schema") holds
for *structural* reads/writes but not for the semantic cases §3 already carves out
(a `NOT NULL server_default` admin column). So "free" is slightly overclaimed: additive
buys structural cumulative-safety; the semantic residual still routes through §3's
review-tag + §4's smoke. Minor — §3 handles it — but §2 shouldn't sell "free" without
the asterisk. Carrying as a wording note for the re-Temper.

## F2.5 — the container-boot smoke is CI-HEAVY and a flakiness risk (carry)

§4 runs in CI: pull two GHCR images, boot one through its entrypoint, probe endpoints.
Network (GHCR pull), container boot timing, and port/health races make this the most
flake-prone gate in the repo — and a flaky *required* gate trains humans to bypass it
(the exact failure the whole design fights). Open question for re-Temper: per-PR
(on `alembic/versions/**` change) vs a **pre-release gate on tag** (fewer runs, the
release image exists by then), and required-but-retryable vs advisory. Carrying.

## F2.6 — A1-before-A2 re-creates v1's structural-only gap under a new name (carry, already §9 Q4)

Shipping A1 (lint) before A2 (smoke) makes *watched* rollback safe but leaves semantics
unproven — which is v1's exact "structural green ≠ safe" gap, just scoped to watched
deploy. Defensible IF #10-unwatched is hard-gated on A2 (§8 says so), but the re-Temper
should pressure whether "watched-only after A1" is a real, enforced state or a doc
promise. Already DESIGN-v2 §9 Q4; flagging that the Fold agrees it's the sharpest
sequencing question.

## Disposition

F2.1, F2.2, F2.3 are clear corrections → folding into DESIGN-v2 now (strengthen before
re-Temper). F2.4, F2.5, F2.6 carried as re-Temper ammunition / sharpened open questions.
The Fold found no frame-level FATAL in v2 — but neither did v1's Fold, and the Temper
found one there, so v2 does not ship on this Fold's say-so either.
