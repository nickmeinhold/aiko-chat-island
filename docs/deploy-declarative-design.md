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
# ISLAND_ID / ISLAND_DISPLAY_NAME / ISLAND_SEED_PEERS …
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
