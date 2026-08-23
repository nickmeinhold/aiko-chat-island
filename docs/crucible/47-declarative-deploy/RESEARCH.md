# RESEARCH (Heat) — declarative island deploy

**Scope**: bounded inline pass (labelled *shallow* — infra plumbing on measured
constraints, not a research frontier). Focus: the risky/non-obvious mechanics the
design rests on. Ground truth from THIS repo + boxes was gathered live (see DESIGN.md
"measured constraints").

## 1. Secret handling — render-locally + ship-plaintext `.env`

- **Model**: secrets live SOPS-encrypted in git (age); decrypted ONLY on the laptop
  (which has the age key); rendered `.env` shipped over scp (ssh transport = encrypted
  in transit); box holds plaintext `.env` (unchanged from today).
- **Known-good**: this is the standard "encrypted at rest in VCS, plaintext at the
  consumer" pattern (SOPS' own recommended flow). The box already holds a plaintext
  `.env` today — the design does NOT worsen the box's at-rest posture; it *improves*
  the repo/transit posture (encrypted at rest, no more hand-typing secrets into ssh).
- **Risk hunted**: does the rendered plaintext `.env` leak on the laptop? Mitigations:
  render into a `mktemp` file with `umask 077`, `shred -u`/`rm` on exit via `trap`,
  never echo secrets to stdout/logs. Docker `env_file`/`environment` already reads the
  box `.env`; no change there.
- **Alternative posture (rejected for now, noted)**: docker/systemd secrets or runtime
  injection would keep the box from ever holding plaintext — but that's a bigger change
  to the compose contract and the boxes lack the tooling; out of scope for the drift fix.

## 2. Atomic whole-file replace over ssh

- **Mechanism**: `scp` to a staging temp path on the box, then `mv staged final` — `mv`
  within the same filesystem is atomic (`rename(2)`), so a reader never sees a partial
  file, and an interrupted transfer leaves the live file untouched.
- **Caveat**: `scp` directly onto the live path is NOT atomic (truncates-then-writes) —
  the staging+`mv` is load-bearing, not optional. This is the structural replacement for
  the in-place `awk`/`sed` edit that caused the truncation.
- **Backup**: capture `cp final final.bak-<ts>` on the box BEFORE the `mv` (structural,
  tool-owned) — so a bad render is one `mv` from rollback.

## 3. Rollback correctness — config vs data

- `update.sh` already backs up the DB (external volume `aiko_data`) fail-closed before
  recreate. It does NOT back up config today.
- The design adds config `.bak-<ts>` on the box before overwrite. So a bad deploy has
  two independent rollbacks: config (`.bak`) and DB (`update.sh`'s pre-update copy).
- **Open**: `deploy-to.sh` ships config THEN runs `update.sh` (which recreates the
  stack). If the new config is bad, the container may fail `/health` — `update.sh`
  surfaces that (it verifies `/health` and exits non-zero). Recovery = restore config
  `.bak` + re-run. Design must make this recovery path explicit, not implicit.

## 4. Partial-deploy / interruption states (for Fold to enumerate)

- Interruption AFTER scp-of-some-files but BEFORE `update.sh`: box has new config, old
  running container → next `update.sh` picks up new config (converges). Safe-ish but
  must be idempotent.
- Interruption DURING the staged `mv` sequence across N files: some files new, some old
  → mixed config. Mitigation: order the `mv`s so compose+`.env` land together, or stage
  all then `mv` all last (minimise the window).
- `sops -d` fails (bad key / corrupt secrets file): must fail-closed BEFORE any scp —
  never ship a half-rendered `.env`.

## 5. Shared-tenant blast radius

- Both boxes are shared multi-tenant (imagineering: dreamfinder/lyra/claude-shim…;
  enspyr: real human tenants). The tool must touch ONLY the island's app dir + its
  compose project (`name: aiko`) — no box-wide `docker` prune, no host-level restarts,
  no writing outside `REMOTE_PATH`. `update.sh`'s `docker compose` scoping already
  respects the project; `deploy-to.sh` must keep every write inside `REMOTE_PATH`.

## Verdict of Heat

No finding dissolves the ore. The design's mechanics are standard and sound; the risk
concentrates in **(a)** the atomicity discipline (staging+`mv`, not direct scp),
**(b)** fail-closed ordering (`sops -d` and render before any ship), and **(c)** making
the config-rollback path explicit. These go to Fold + Temper.
