#!/usr/bin/env bash
#
# update.sh — safely update a running island to the latest published image.
#
# The whole point of the published image: updating is `docker compose pull && up`.
# This script wraps that with the two things you should never skip on a box that
# holds real data:
#
#   1. BACK UP the sole-copy SQLite store FIRST (online hot copy), fail-closed —
#      if the backup doesn't land, we abort BEFORE touching the running stack.
#   2. VERIFY /health after the recreate (the entrypoint migrates fail-closed, so
#      a bad migration keeps the container from serving — we surface that).
#
# Usage:
#   deploy/update.sh                 # backup -> pull -> up -d -> verify
#   deploy/update.sh --from-source   # backup -> build from this checkout -> up -> verify
#   deploy/update.sh --no-backup     # skip the backup (only if you back up elsewhere)
#   deploy/update.sh --yes           # non-interactive (no confirm prompt)
#
# Pin a version by exporting ISLAND_VERSION (e.g. ISLAND_VERSION=v0.1.0) or setting
# it in .env; default is `edge` (tracks main).

set -euo pipefail

c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
log()  { printf '%s==>%s %s\n'  "$c_bold" "$c_rst" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_grn"  "$c_rst" "$*"; }
warn() { printf '%s warn%s %s\n' "$c_ylw" "$c_rst" "$*" >&2; }
die()  { printf '%s fail%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
[ -f docker-compose.yml ] || die "docker-compose.yml not found in $REPO_ROOT"

FROM_SOURCE="false"; DO_BACKUP="true"; INTERACTIVE="true"
while [ $# -gt 0 ]; do
  case "$1" in
    --from-source) FROM_SOURCE="true"; shift ;;
    --no-backup)   DO_BACKUP="false"; shift ;;
    --yes)         INTERACTIVE="false"; shift ;;
    -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'; exit 0 ;;
    *)             die "unknown argument: $1 (see --help)" ;;
  esac
done

command -v docker >/dev/null 2>&1 || die "missing required tool: docker"

# Elevation autodetect: some island hosts run the deploy user OUTSIDE the docker
# group (enspyr/nick-mel: user 'ubuntu' needs `sudo -n docker`), while others are
# in-group (imagineering: bare docker works). Probe once and route every docker
# call through $DOCKER so this script runs natively on BOTH without a manual shim.
# `sudo -n` is non-interactive: if elevation is needed but passwordless sudo isn't
# configured, fail closed with a clear message rather than a cryptic permission error.
DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  if sudo -n docker ps >/dev/null 2>&1; then
    DOCKER="sudo -n docker"
  else
    die "docker needs elevation on this host but 'sudo -n docker' failed — add the deploy user to the 'docker' group, or configure passwordless sudo for docker"
  fi
fi

$DOCKER compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# The island must actually be running (this is an UPDATE, not a first standup).
$DOCKER compose ps --status running --services 2>/dev/null | grep -qx chat-island \
  || die "the 'chat-island' service isn't running — use deploy/standup.sh for a first standup"

# --- preflight: a PARTIAL APNS_* set now refuses to boot --------------------
#
# claude-tasks#3366 (cage-match PR#141 round 3, Tesla — an accepted risk, recorded
# rather than absorbed). APNS_* used to be dead ink on a host: compose did not
# forward it, so a half-drafted credential set (key id and team id pasted in, the
# private key still to come) sat harmlessly in .env and the island booted with push
# simply off. PR#141 made compose forwarding total, so those bytes now reach the
# container — where config.py's half-configured guard deliberately REFUSES TO BOOT.
#
# The guard is right and must not be weakened: a partial set reads as "push is on"
# at every call site while every send fails at Apple's door, and on a handset a
# missed call is indistinguishable from a disabled feature. What changed is WHEN it
# fails — at boot instead of never. With `restart: always` that is a crash-loop, on
# a box nobody touched, triggered by a version bump rather than by an edit.
#
# So catch it HERE, before the backup and before anything is pulled: an operator
# reading this message still has a running island.
# Extracted to its own script so it can be tested directly rather than only in situ
# (tests/test_deploy_preflight.py exercises the all/none/partial arms). A check that
# cannot be run in isolation tends to be a check nobody proves can fail.
if [ -f "$SCRIPT_DIR/preflight-apns.sh" ]; then
  # Present but not executable is a BROKEN install, not a legacy box — fail rather
  # than skip (cage-match PR#148, Carnot MEDIUM).
  [ -x "$SCRIPT_DIR/preflight-apns.sh" ] \
    || die "preflight-apns.sh exists but is not executable — refusing to deploy with a disabled safety check. chmod +x it."
  "$SCRIPT_DIR/preflight-apns.sh" "$REPO_ROOT/.env" \
    || die "APNs preflight failed (see above) — aborting BEFORE the backup; the island is still running"
else
  # ABSENT means an older copy of this tree. Do NOT fail — that would block a deploy
  # on a box whose update.sh predates the check, which is a worse outcome than the
  # crash-loop it guards. But say so LOUDLY: the boxes most likely to be missing it
  # are exactly the drifted ones it was written to protect (#2301 — update.sh's copy
  # on each box is a separate artifact and does not sync itself). A silent skip here
  # would let the guarantee evaporate precisely where it is needed, with no signal.
  warn "APNs preflight NOT FOUND ($SCRIPT_DIR/preflight-apns.sh) — this box's deploy
     tree predates it. A PARTIAL APNS_* set in .env will crash-loop the island after
     the recreate. Check by hand, or refresh this box's deploy/ from the repo."
fi

# --- step 1: back up the sole-copy DB (fail-closed) -------------------------
if [ "$DO_BACKUP" = "true" ]; then
  log "Step 1/3 — backing up the SQLite store (online hot copy) BEFORE any change"
  backup_dir="$REPO_ROOT/backups"; mkdir -p "$backup_dir"
  # Timestamp comes from the HOST shell (the container is slim; keep it simple).
  ts="$(date +%Y%m%d-%H%M%S)"
  # Online .backup() inside the container (no sqlite3 CLI in the slim image), then
  # copy the artifact out and remove the in-container temp. integrity_check gates.
  $DOCKER compose exec -T chat-island python -c "
import sqlite3, sys
src = sqlite3.connect('/data/aiko.db')
dst = sqlite3.connect('/data/_update-$ts.db')
with dst: src.backup(dst)
res = dst.execute('PRAGMA integrity_check').fetchone()[0]
print('integrity_check:', res)
sys.exit(0 if res == 'ok' else 1)
" || die "backup integrity_check failed — ABORTING before touching the stack"
  $DOCKER compose cp "chat-island:/data/_update-$ts.db" "$backup_dir/aiko.db.preupdate-$ts" \
    || die "could not copy the backup out of the container — ABORTING"
  $DOCKER compose exec -T chat-island rm -f "/data/_update-$ts.db" || true
  sz=$(wc -c < "$backup_dir/aiko.db.preupdate-$ts" | tr -d ' ')
  [ "${sz:-0}" -gt 4096 ] || die "backup file is implausibly small ($sz bytes) — ABORTING"
  ok "backed up to backups/aiko.db.preupdate-$ts ($sz bytes, integrity ok)"
else
  warn "Step 1/3 — backup SKIPPED (--no-backup). You are responsible for a current backup."
fi

# --- confirm before the irreversible recreate ------------------------------
if [ "$INTERACTIVE" = "true" ]; then
  printf 'Proceed with pull + recreate? [y/N] '
  read -r ans; case "$ans" in y|Y|yes) ;; *) die "aborted by user (backup, if taken, is kept)";; esac
fi

# --- step 2: update the image + recreate ------------------------------------
if [ "$FROM_SOURCE" = "true" ]; then
  log "Step 2/3 — building the island image from source + recreating"
  $DOCKER compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
else
  log "Step 2/3 — pulling the latest published image + recreating"
  $DOCKER compose pull
  $DOCKER compose up -d
fi
ok "stack recreated (entrypoint migrates fail-closed before serving)"

# --- step 3: verify ---------------------------------------------------------
log "Step 3/3 — verifying /health (a failed migration keeps the container from serving)"
for _ in $(seq 1 30); do
  if health="$(curl -fsS --max-time 3 http://127.0.0.1:8095/health 2>/dev/null)"; then
    ok "gateway healthy on 127.0.0.1:8095 — update complete 🎉"
    # Say WHICH CODE is now serving. This script pulls an image and does not sync
    # docker-compose.yml (#2301), so "the deploy succeeded" has never been the same
    # claim as "the intended commit is running" — on 2026-08-29 the deploy tree was
    # a month stale and nothing said so. /health now carries the provenance baked
    # into the image at build time, so the answer comes from the running container
    # rather than from a file on the host beside it.
    # Parsed with sed, not jq: jq is not guaranteed on an island box, and this is
    # informational — a parse miss must never fail a good deploy.
    sha="$(printf '%s' "$health" | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p')"
    ref="$(printf '%s' "$health" | sed -n 's/.*"ref":"\([^"]*\)".*/\1/p')"
    if [ -n "$sha" ]; then
      ok "running ${ref:-(no ref)} @ ${sha}"
    else
      warn "this image reports no build provenance — a local build, or an image from
       before /health carried it. Compare against the tag you meant to deploy by hand."
    fi
    exit 0
  fi
  sleep 2
done
die "gateway did not answer /health within ~60s after update. Inspect: docker compose logs chat-island
     (restore path: the pre-update backup is in backups/ — see docs/deploy-passkeys-runbook.md)"
