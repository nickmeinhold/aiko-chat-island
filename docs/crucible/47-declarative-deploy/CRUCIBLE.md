# CRUCIBLE — declarative island deploy (#47 / #1577)

## The pick (pre-selected by Nick, 2026-07-28)

Not scouted — Nick chose it after we measured the drift live. The ore is real: two
live islands, deploy-by-hand over ssh, and an `awk`-over-ssh quoting mishap that
**zeroed enspyr's live `docker-compose.yml` to 0 bytes** mid-fix last session (PR#103),
recovered only by a `cp` backup taken out of habit.

## Why it glows AND what it changes

- **Removes a real recurring failure surface**, not adds a feature: every deploy today
  is `scp`/`sed`/`--yes` over ssh with backup-by-discipline. The tool makes backup
  structural and in-place mutation impossible.
- **Turns "repo-authoritative" from a fiction into a fact.** Right now neither box is a
  git checkout — config reaches them by hand and silently drifts (the `.env`
  key-sets already diverge between the two islands).
- **High transfer**: every small self-hosted fleet hits the "too small for Kubernetes,
  too big for scp" band. The right-sized answer is genuinely non-obvious.

## The falsifier (what, if true, makes this ore slag)

**If the whole config layer is already effectively in sync and the drift risk is
one-off human error, then the elegant fix is a 3-line "edit the repo, `scp` the whole
file" discipline note — not a rendering+manifest+SOPS toolchain.** The measured facts
push back (the `.env` key-sets *do* diverge; neither box is a checkout; the truncation
already happened once) — but the adversary should test whether the design is heavier
than the demonstrated risk warrants. Over-engineering is the live failure mode here,
because the island ethos is design-for-subtraction and operator-ergonomics.

## Load-bearing enthusiasm assumptions (for Temper to strike)

1. Control-side render + whole-file ship is *simpler AND safer* than the alternatives —
   not just different.
2. Plaintext `.env` on the box (unchanged from today) is an acceptable secret posture
   given the age key never leaves the laptop.
3. The "render must byte-match live `.env` before first deploy" gate makes the cutover a
   true config no-op (only delivery changes), so the rollout is low-risk.
4. A per-island shell `.conf` manifest is enough structure — not so little it drifts
   again, not so much it becomes a framework.
