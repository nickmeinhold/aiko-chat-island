# Temper — cross-family strike on the reactive-deploy design

*Movement 5. Cross-family design cage-match on CRUCIBLE + RESEARCH + DESIGN.
Seated: Kelvin (Gemini-3-Pro), Carnot (GPT/Codex), Tesla (Grok). Wu (Kimi) dark-seated
(CLI model-config). 3-family strike, STRONG convergence.*

## Verdict: FATAL (as cast) — Kelvin FATAL · Tesla FATAL · Carnot REQUEST_CHANGES

The design **fails its own falsifier**. All three families independently struck the
SAME load-bearing flaw the author's Fold laundered past (same-distribution blindness —
the Fold reasoned about image/digest mechanics and never saw the state coupling).

## The fatal flaw (convergent, load-bearing)

**Image-only rollback ≠ restore previous serving state, for a self-migrating,
data-holding service.**

The failure cascade (Kelvin's numbering; Tesla's table; Carnot's "rollback means
previous digest is false for data-holding boxes"):
1. Release v2 contains a DB migration; running is v1.
2. `update.sh` backs up the DB at schema N (good) — but the backup is only ever a
   PREFLIGHT, never a recovery step.
3. v2 entrypoint migrates N→N+1 (immediate, persistent on the `aiko_data` volume).
4. v2 fails `/health` (or the digest assert).
5. Rollback re-pins the OLD image (`:lastgood`) — but the volume is STILL at N+1.
6. v1 code against schema N+1: crash-loop, or silent data corruption, or a false-green
   if health is shallow.
7. → "ROLLBACK FAILED — manual intervention", now with a **forward schema + old binary**
   — strictly WORSE than never having deployed.

**"Backup without restore-on-failure is a souvenir, not a spine"** (Tesla). The design's
central safety claim ("auto-rollback makes deploys safer than today") is inverted: it
manufactures a guaranteed outage on the first migrating bad release.

## Additional severe findings (survive even if the DB flaw were fixed)

- **The "healthy" bad release is the DANGER mode, not the failure mode** (Tesla B,
  Kelvin). A subtly-broken release that returns `/health` 200 (logic regression, auth
  hole, bad LLM replies) gets PROMOTED to both boxes with no rollback. Manual deploy has
  a human smoking a real path; unwatched auto-deploy removes exactly that cheap safety.
  A deeper readiness probe (`SELECT 1`) does NOT close this — it still greens on N+1 with
  the wrong binary until a real incompatible path is exercised. **This is a limit of
  unwatched auto-deploy itself, not a health-check depth bug.**
- **Simultaneous dual-prod deploy = guaranteed GLOBAL outage on every bad release**
  (Kelvin blast-radius). Per-box rollback bounds DURATION, not OCCURRENCE. Accepting a
  user-facing global outage as the v1 baseline under-counts blast radius; a sequenced
  (canary) deploy prevents it entirely.
- **False quarantine — the inverse of thrash** (Tesla C). The Fold's `failed.digest` fix
  creates its own failure: a TRANSIENT failure (GHCR blip mid-pull, OOM, flock race,
  host fault) quarantines a GOOD release, which is then never retried until a human
  clears state. Blaming the image for an env fault hides the real cause.
- **Lock principal** (Tesla, research §5 open Q): the `flock` protects nothing unless the
  timer's `User=` == the human's principal on the `sudo -n docker` box.

## Disposition: the CHOSEN SHAPE is slag; the CANDIDATE (reduce deploy toil) survives

This does NOT prove "reduce deploy toil" is slag — it proves **unwatched auto-deploy of
a self-migrating stateful service** is slag. The reframe (Tesla's + Kelvin's closing):
the human watching `/health` after a manual deploy, and the human judgment on a
migration, are CHEAP SAFETY that the "islands react (unwatched)" frame throws away. What
Nick actually wants — less toil — is separable from removing the human.

Because the fatal finding challenges the **frame Nick explicitly chose** (pull-watcher +
unwatched auto-deploy), re-casting to a safe shape is a **frame re-pick that is Nick's
call**, not an author auto-re-cast (that would be grading my own homework on a decision
Nick owns). Per the crucible's honest-negative-result rule: **STOP at Temper, report the
fatal finding + the reframe, hand the re-pick to Nick.** The ≤3 recast rounds are
reserved for after Nick re-picks the shape.

## The safe-shape fork (for Nick's re-pick)

1. **One-command sequenced deploy** (recommended): a laptop `deploy/release.sh vX.Y.Z`
   that deploys imagineering → verifies → deploys enspyr, human watching, with a rollback
   that restores the DB backup (or requires N-1-compatible migrations). Removes the TOIL
   (the multi-step ssh dance) while keeping the human-in-loop safety. Not "islands react"
   — "one button, sequenced, watched."
2. **Migration-gated auto-deploy**: auto-deploy ONLY migration-free releases (image-only
   rollback IS safe for those); a release carrying a migration notifies "manual deploy
   required". Keeps reactivity for the safe subset; needs a migration-detector.
3. **Expand/contract migration discipline first** (prerequisite project): require every
   migration backward-compatible (N-1) + a CI check, which makes image-only rollback
   genuinely safe — THEN unwatched reactivity is defensible. Bigger, principled.
4. Two of these compose (e.g. 1 now, 3 as the enabler for a future 2).
