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
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# The island must actually be running (this is an UPDATE, not a first standup).
docker compose ps --status running --services 2>/dev/null | grep -qx chat-island \
  || die "the 'chat-island' service isn't running — use deploy/standup.sh for a first standup"

# --- step 1: back up the sole-copy DB (fail-closed) -------------------------
if [ "$DO_BACKUP" = "true" ]; then
  log "Step 1/3 — backing up the SQLite store (online hot copy) BEFORE any change"
  backup_dir="$REPO_ROOT/backups"; mkdir -p "$backup_dir"
  # Timestamp comes from the HOST shell (the container is slim; keep it simple).
  ts="$(date +%Y%m%d-%H%M%S)"
  # Online .backup() inside the container (no sqlite3 CLI in the slim image), then
  # copy the artifact out and remove the in-container temp. integrity_check gates.
  docker compose exec -T chat-island python -c "
import sqlite3, sys
src = sqlite3.connect('/data/aiko.db')
dst = sqlite3.connect('/data/_update-$ts.db')
with dst: src.backup(dst)
res = dst.execute('PRAGMA integrity_check').fetchone()[0]
print('integrity_check:', res)
sys.exit(0 if res == 'ok' else 1)
" || die "backup integrity_check failed — ABORTING before touching the stack"
  docker compose cp "chat-island:/data/_update-$ts.db" "$backup_dir/aiko.db.preupdate-$ts" \
    || die "could not copy the backup out of the container — ABORTING"
  docker compose exec -T chat-island rm -f "/data/_update-$ts.db" || true
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
  docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
else
  log "Step 2/3 — pulling the latest published image + recreating"
  docker compose pull
  docker compose up -d
fi
ok "stack recreated (entrypoint migrates fail-closed before serving)"

# --- step 3: verify ---------------------------------------------------------
log "Step 3/3 — verifying /health (a failed migration keeps the container from serving)"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8095/health >/dev/null 2>&1; then
    ok "gateway healthy on 127.0.0.1:8095 — update complete 🎉"
    exit 0
  fi
  sleep 2
done
die "gateway did not answer /health within ~60s after update. Inspect: docker compose logs chat-island
     (restore path: the pre-update backup is in backups/ — see docs/deploy-passkeys-runbook.md)"
