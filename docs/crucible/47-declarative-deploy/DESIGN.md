# Declarative island deploy — design (#47 / #1577)

Status: **design** (2026-07-28). Grounded on measured drift + SOPS reality, not intent.
Supersedes the hand-`scp`/`sed`/`--yes` deploy that zeroed enspyr's live compose (the
`awk`-over-ssh truncation near-miss, PR#103).

## What's already solved (don't rebuild)

- **Image layer**: pinned `ISLAND_VERSION`, published multi-arch to GHCR, `compose pull`.
- **compose file**: byte-identical across both boxes AND repo HEAD (`6f7c27da…`).
- **Box-side `update.sh`**: backup-DB-first fail-closed → pull → up → verify `/health`,
  with docker-elevation autodetect (`docker` on imagineering, `sudo -n docker` on enspyr).
  This stays as-is and remains the box-side executor.

## What actually drifts (the target)

The **config layer**, reaching the box by hand:
- `.env` — divergent key sets across boxes, secret-bearing, edited in place (the truncation surface).
- Neither box is a git checkout — "repo-authoritative" is currently a fiction maintained by hand-copying.
- Per-box path/user divergence (`nick@…/apps/aiko-chat-gateway` vs `ubuntu@…`).

## Measured constraints that shape the design

- **SOPS**: present on the **laptop** (sops 3.11.0 + age key), **absent on both boxes**.
  ⇒ **render control-side**, ship a complete `.env`; the age key never leaves the laptop.
- **Boxes are shared, multi-tenant** (imagineering: dreamfinder/lyra/…; enspyr: real human
  tenants). ⇒ tool must be a good tenant: no box-wide ops, per-app scope only.
- **DB is an external named volume** (`aiko_data`), compose project name pinned `aiko`
  ⇒ moving/rewriting config files never touches the running store.

## The design: control-side render + whole-file ship, box-side `update.sh`

Three repo-authoritative inputs per island + two scripts.

### Repo-authoritative inputs

```
deploy/
  islands/<island>.conf     # per-island NON-SECRET manifest (shell-sourced)
  secrets/<island>.enc.env  # SOPS-encrypted secrets (JWT_SECRET, GITHUB_CLIENT_SECRET…)
  .env.template             # the shape: ${VARS} referencing manifest + secrets
  update.sh                 # box-side executor (EXISTS, unchanged)
  mosquitto.conf, caddy/    # already repo-authoritative
docker-compose.yml          # already in sync
.sops.yaml                  # age recipients (creation rule for secrets/*.enc.env)
```

`islands/<island>.conf` declares everything that legitimately differs per island —
and is NOT secret:

```sh
ISLAND=enspyr
SSH_ALIAS=nick-mel          # connection
REMOTE_USER=ubuntu          # user-agnostic: nick on imagineering, ubuntu on enspyr (until infra migration)
REMOTE_PATH=/home/ubuntu/apps/aiko-chat-gateway
DOMAIN=chat.enspyr.co
PASSKEY_RP_ID=chat.enspyr.co
ISLAND_VERSION=0.2.4
SOCIAL_SIGNIN_ENABLED=false
# GATEWAY_ID / GATEWAY_DISPLAY_NAME / GATEWAY_SEED_PEERS …
```

### `deploy/deploy-to.sh <island>` — NEW, runs on the laptop (control plane)

1. `source deploy/islands/<island>.conf` (non-secret config).
2. `sops -d deploy/secrets/<island>.enc.env` → secrets into shell env (laptop has the key).
3. Render `deploy/.env.template` → a temp `.env` locally (`envsubst` / explicit map).
4. **Whole-file `scp`** compose + rendered `.env` + `update.sh` + `mosquitto.conf` to
   `${REMOTE_USER}@${SSH_ALIAS}:${REMOTE_PATH}` — via a staging temp then atomic `mv`,
   keeping a timestamped `.bak-<ts>` of the previous config on the box (structural backup,
   not remembered). **Never** an in-place stream edit — this is what kills the awk class.
5. `ssh <box> 'cd <path> && ./update.sh --yes'` — the existing box-side flow (DB backup →
   pull → up → verify `/health`).
6. `shred`/rm the local temp `.env`.

### Properties

- **Repo-authoritative for real**: every value on the box has a source in the repo
  (manifest or SOPS). A deploy overwrites config wholesale — the box cannot silently diverge.
- **Secrets encrypted at rest** in git; decrypted only on the laptop; the boxes never hold
  the age key (cleaner blast radius than putting keys on shared boxes).
- **Whole-file ship** — the truncation class is structurally impossible.
- **User-agnostic** — manifest declares `REMOTE_USER`/`REMOTE_PATH`; works on both boxes
  today, and the infra-tab `ubuntu→nick` migration is a one-line manifest flip afterward.
- **Structural backup** — config `.bak-<ts>` on the box + the DB backup `update.sh` already does.

## Rollout (current → declarative, low-risk because config is UNCHANGED)

1. **Lift current reality into the repo**: read each box's live `.env` (already inventoried),
   derive `islands/<island>.conf` (non-secret) + extract secrets into `secrets/<island>.enc.env`
   (SOPS-encrypt with the laptop age key). The boxes are the current source of truth for the
   *values*; we're capturing them, not changing them.
2. **Prove the render reproduces reality**: render each island's `.env` and `diff` it against
   the box's live `.env` (modulo ordering). Gate: **do not deploy until the rendered `.env`
   byte-matches what's live.** First declarative deploy is then a config no-op — only the
   *delivery mechanism* changes, so nothing behavioural moves.
3. Commit manifests + `.sops.yaml` + encrypted secrets.
4. First `deploy-to.sh <island>` = whole-file ship of the verified-matching config + `update.sh`.

## Open sub-decisions (defaults proposed)

- **Secrets layout**: per-island `secrets/<island>.enc.env` (JWT differs per island; imagineering
  has social secrets enspyr lacks). *Default: per-island.*
- **Manifest format**: shell `.conf` sourced directly (no parser dep on a slim toolchain). *Default: shell.*
- **Render tool**: `envsubst` (gettext) vs an explicit bash var map. *Default: explicit map (no new dep, fail-closed on a missing var).*

## Review posture

Secrets + live-prod delivery = trust-boundary-adjacent → the BUILD gets a `/cage-match`
(injection/secret-exfil/whole-file-atomicity lens) before it deploys, not solo self-review.

## Claims to falsify (for Temper)

1. **Control-side render is simpler AND safer** than the alternatives — not just different.
2. **Plaintext `.env` on the box is acceptable** given the age key never leaves the laptop
   and the box already holds a plaintext `.env` today (no regression).
3. **The byte-match gate makes the cutover a true config no-op** — only delivery changes.
4. **A shell `.conf` manifest is the right amount of structure** — not so little it drifts,
   not so much it's a framework.
5. **Whole-file `scp` + staged `mv` is atomic enough** that no reader/recreate ever sees a
   partial config.

## Rejected alternatives (simpler shapes passed over)

- **Discipline note only** ("edit repo, `scp` whole file, never `sed` live"): rejected
  because it doesn't make repo-authority *true* (nothing renders the divergent `.env`
  from one source) and relies on the same human memory that just truncated a file.
- **Full IaC (Ansible/Terraform)**: rejected — miles oversized for 2 shared boxes where the
  image layer is already solved; violates design-for-subtraction.
- **On-box SOPS decrypt**: rejected — boxes lack sops + age key (measured); putting the age
  key on shared multi-tenant boxes is a worse blast radius than laptop-only decrypt.
- **git-checkout-on-box + `git pull` deploy**: rejected — reintroduces a checkout to drift/
  dirty on a shared box, and secrets still need a separate channel; the pull model is what
  we're replacing.

## Fold — author's adversarial self-pass (pre-Temper, no round budget)

Degenerate/failure states enumerated and resolved into the design:

- **`sops -d` fails (bad/absent key, corrupt secrets)** → FAIL CLOSED before any scp. No
  partial ship. `deploy-to.sh` aborts with non-zero; box untouched.
- **Missing var in template** → explicit-map render fails closed (a `${VAR}` with no source
  is an abort, not an empty string — an empty `JWT_SECRET` would crash-boot the container,
  but fail-closed at render is cheaper than fail-closed at boot).
- **Interrupted mid-ship (some files new, some old)** → stage ALL files to temp paths, then
  `mv` them in one tight sequence, compose+`.env` adjacent; a crash before the `mv`s leaves
  the live config wholly untouched (still the old, working set).
- **Bad render that byte-differs from live on FIRST cutover** → the rollout gate refuses to
  deploy until the rendered `.env` byte-matches the live `.env` (modulo key ordering). This
  is the load-bearing safety property; the first deploy is a no-op by construction.
- **Bad render on a LATER deploy (config genuinely changed)** → config `.bak-<ts>` captured
  on the box before the `mv`; `update.sh` verifies `/health` and exits non-zero on failure;
  recovery = restore `.bak` + re-run. This recovery path is now explicit (was implicit).
- **`n=0` islands / unknown island name** → `deploy-to.sh <island>` with no matching
  `islands/<island>.conf` aborts (no default target — fail closed, never deploy to a guessed
  box).
- **Secret leak on laptop** → render into `mktemp` (umask 077), `trap`-shred on exit, never
  log secret values.
- **Shared-tenant blast** → every write confined to `REMOTE_PATH`; no box-wide docker/host
  ops; `update.sh`'s compose scoping (`name: aiko`) already isolates the project.

**Did NOT dissolve the ore.** The simplest rejected alternative (discipline note) fails the
repo-authority requirement, so the toolchain earns its weight — but the Fold *did* surface
that the design's real load is concentrated in **fail-closed ordering + staged-mv atomicity +
an explicit config-rollback path**, which are now written in rather than assumed. Remaining
open for the cross-family strike: is the byte-match gate *sufficient* proof of no-op, or can
config differ in ways a byte-diff misses (env ordering, whitespace, compose interpolation)?

---

# v2 — POST-TEMPER RE-CAST (2026-07-28)

The cross-family strike (TEMPER.md) cracked v1 on two load-bearing claims (byte-match
gate, staged-mv atomicity) and named a subtractive middle Fold missed. v2 supersedes the
sections above where they conflict. **Core unchanged** (truncation-class fix, laptop-only
age key, `update.sh` reuse); **render layer deleted**, **atomicity re-based on a generation
symlink**.

## The shift in one line

v1 shipped a *renderer* (template + secrets → `.env`) and then tried to *prove the render
faithful* (byte-match gate). v2 ships **files**: the complete `.env` lives encrypted in git,
so the artifact in git IS the artifact on the box — no render, no proof needed.

## Repo-authoritative inputs (v2)

- `deploy/secrets/<island>.enc.env` — SOPS-encrypted **COMPLETE** `.env` (the exact bytes
  to ship). No template, no `envsubst`, no missing-var map.
- `deploy/islands/<island>.conf` — tiny NON-SECRET connect manifest (`SSH_ALIAS`,
  `REMOTE_USER`, `REMOTE_PATH`, `ISLAND_ID`), parsed as **DATA** (strict `KEY=VALUE`
  allowlist + validation), **never `source`d**.
- `.sops.yaml`; box-side `deploy/update.sh` unchanged.

## `deploy-to.sh <island>` (v2)

1. Parse manifest as data (allowlisted keys; validate `REMOTE_PATH`, `SSH_ALIAS`, `ISLAND_ID`).
2. `set +x`; `sops -d secrets/<island>.enc.env --output "$TMP"` → decrypt to a `mktemp`
   file with `umask 077`. **Never** load secrets into shell env or argv. `trap 'shred -u $TMP'`
   on all exits (SIGKILL residue = named accepted risk).
3. **Tenant preflight** (before any write): ssh box, confirm `REMOTE_PATH/current` is THIS
   island — compose project `name: aiko` present AND `ISLAND_ID`/`DOMAIN` matches the manifest.
   Refuse on mismatch (never deploy to a guessed/wrong box on a shared tenant host).
4. Ship the cohort into `REMOTE_PATH/releases/<ts>/` (whole files: compose, decrypted `.env`,
   `update.sh`, `mosquitto.conf`) — staging **under `REMOTE_PATH`** (same filesystem as
   `current`, so the flip is a true rename, not copy+unlink).
5. **Atomic cohort switch**: `ln -sfn releases/<ts> REMOTE_PATH/current.new && mv` (atomic
   `rename(2)`) `current.new → current`. ONE syscall switches the entire config set; a crash
   before it leaves the old `current` wholly intact.
6. `ssh box 'cd REMOTE_PATH/current && ./update.sh --yes'` (DB backup → pull → up → `/health`).
7. Shred temp; **prune** old `releases/<ts>/` beyond the last N (they hold plaintext `.env` —
   capped retention on the shared box).

## Cutover gate (v2) — direct real-object diff, not a render proof

First cutover: `sops -d` the complete `.env` and **`diff` it directly against the box's live
`.env`** (the real object we're about to replace, lifted verbatim into the SOPS secret). A
clean diff means the shipped bytes equal the live bytes — a genuine delivery-path exercise.
**Scope honestly** (folded from Tesla): step 6 still runs `update.sh` (pull → recreate), so
"no-op" is CONFIG-scoped, not runtime; the first run exercises the recreate path too. Optionally
the first run may skip the pull to prove *delivery* in isolation.

## Rollback (v2) — generation-addressed

`restore <ts>` = `ln -sfn releases/<ts> current.new && mv current.new current` + re-run
`update.sh`. The config cohort is ONE unit (a release dir), so no wrong-generation recombination.
**Named limits** (not auto-covered): a config rewind does NOT undo image/migration/volume
effects — a deploy that coupled `ISLAND_VERSION` to a migration needs the DB backup
(`update.sh`'s pre-update copy) too; `/health` green ≠ correct config (silent misconfig like a
wrong `PASSKEY_RP_ID` is out of auto-rollback scope — operator judgment).

## Blast-radius / tenant (v2)

Tenant preflight (step 3) + every write under `REMOTE_PATH` + compose treated as a privilege
boundary (fixed project `name: aiko`, no `prune`, no host-service restart, no external-network
mutation) + capped release/`.bak` retention (plaintext-secret-bearing). `sudo -n docker` on
enspyr is root-equivalent authority — the preflight is what stops a wrong `REMOTE_PATH` from
clobbering a co-tenant.

## Falsifier — answered by subtraction, not defense

The toolchain is now LIGHTER than v1 (render layer gone). It's "complete encrypted `.env` per
island + atomic generation flip + tenant preflight." Still more than a 3-line discipline note —
but the note genuinely fails repo-authority (the `.env` DOES diverge across boxes, measured),
and v2 IS the subtractive middle the ethos wanted. We answered the over-engineering strike by
removing the compiler, not by justifying it.
