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
#   deploy/update.sh --skip-drift-check  # deploy despite a drifted deploy tree (LOUD)
#
# The image is pinned in image.env beside docker-compose.yml, one full reference:
#   ISLAND_IMAGE=ghcr.io/nickmeinhold/aiko-chat-island:0.17.1
# To take a release, edit that line and run this script. There is no default: a
# missing pin refuses rather than tracking main (claude-tasks#5165).

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

FROM_SOURCE="false"; DO_BACKUP="true"; INTERACTIVE="true"; DRIFT_CHECK="true"
while [ $# -gt 0 ]; do
  case "$1" in
    --from-source) FROM_SOURCE="true"; shift ;;
    --no-backup)   DO_BACKUP="false"; shift ;;
    --yes)         INTERACTIVE="false"; shift ;;
    --skip-drift-check) DRIFT_CHECK="false"; shift ;;
    -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;/^set -euo/d'; exit 0 ;;
    *)             die "unknown argument: $1 (see --help)" ;;
  esac
done

command -v docker >/dev/null 2>&1 || die "missing required tool: docker"

# Elevation autodetect and the one compose invocation live in a sourced lib, shared
# with deploy/compose.sh (the operator's hand path), so the two cannot drift.
ISLAND_ROOT="$REPO_ROOT"
# A partial sync (update.sh without deploy/lib/) must say so, the same way an absent
# drift guard does below. Unguarded, the `.` fails with a bare "No such file or
# directory" before any check can explain (Maxwell, PR#401 r1).
[ -f "$SCRIPT_DIR/lib/island-compose.sh" ] || die "deploy/lib/island-compose.sh NOT FOUND.
     This update.sh needs it, so deploy/ was synced PARTIALLY (the 2026-09-11 shape).
     Sync the whole of deploy/ from the tag and re-run."
# shellcheck source=lib/island-compose.sh
. "$SCRIPT_DIR/lib/island-compose.sh"
island_docker_detect || die "cannot run docker (see above)"

$DOCKER compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# EVERY COMPOSE CALL IS PINNED TO docker-compose.yml, and this one flag replaced
# three rounds of guards (cage-match rounds 5-7).
#
# A bare `docker compose` picks its own files from the working directory, and
# MEASURED on the live box it picks more than you would guess: `compose.yaml`
# beside `docker-compose.yml` WINS OUTRIGHT (config emitted compose.yaml's
# variables and ignored the other entirely), an override file is merged in, and
# COMPOSE_FILE can redirect the whole set from the environment or from .env.
# Each of those was found as a separate fail-open — the drift guard certifying
# one file set while the engine burned another — and each was met with its own
# refusal.
#
# Pinning ends the class instead of enumerating it. Same measurement, one row
# down: `-f docker-compose.yml` ignores compose.yaml, ignores the override, and
# ignores COMPOSE_FILE. The file set the guard compares is now the file set that
# deploys, BY CONSTRUCTION rather than by prediction — which is the difference
# between an invariant and a guard someone has to keep extending.
#
# Safe on both islands today: neither carries compose.yaml, an override, or
# COMPOSE_FILE (checked), so the resolved config is byte-identical to a bare
# invocation. The guard still WARNS if any such file appears, because a human
# typing `docker compose` by hand here would still get it.
#
# The pin is in image.env, not .env (claude-tasks#5165), and island_compose passes
# both. See deploy/lib/island-compose.sh for why a bare call now refuses.
COMPOSE="island_compose"
island_require_pin || die "no usable image pin (see above) — nothing was changed"
# ONE PIN PER RUN: every compose call below reads this snapshot, not the live file.
island_pin_begin || die "could not snapshot image.env — nothing was changed"
trap 'rm -f "$_island_pin_file"' EXIT
if LC_ALL=C grep -aqE '^[[:space:]]*(export[[:space:]]+)?ISLAND_VERSION[[:space:]]*=' "$REPO_ROOT/.env" 2>/dev/null; then
  warn ".env still carries an ISLAND_VERSION line. It is IGNORED: the image comes
     from image.env. Remove it at the next SOPS re-lift so nobody edits it expecting
     an effect."
fi

# Resolve the whole config FIRST, with its errors visible. The running-check below
# discards compose's stderr, so an image.env that is readable but carries no usable
# ISLAND_IMAGE (empty, a typo'd key) would trip `${ISLAND_IMAGE:?}` THERE and be
# reported as "isn't running, use standup.sh", sending the operator from the
# backup-first door to one that takes no backup (Tesla, PR#401 r1). Asking compose
# keeps compose the only reader of the pin.
$COMPOSE config --quiet || die "compose cannot resolve this island's config (see above) — nothing was changed"

# The island must actually be running (this is an UPDATE, not a first standup).
$COMPOSE ps --status running --services 2>/dev/null | grep -qx chat-island \
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

# --- preflight: the box's deploy tree must MATCH the tag being pulled -------
#
# claude-tasks#4230, and the direct fix for the outage of 2026-09-11. This script
# pulls an IMAGE and does not sync docker-compose.yml — the box's copy is a
# separate artifact (#2301). APNS_VOIP_TOPIC became a required member of the
# all-or-none APNs group; enspyr's .env HAD it and enspyr's compose did not
# FORWARD it, so the value could never reach the container and the island refused
# to boot for several minutes. The box's compose differed from the tag by exactly
# one line. imagineering had the identical gap and deployed clean, because its
# compose happened to get synced first — same change, same drift, opposite
# outcome, one variable.
#
# The APNs preflight above could not have caught it: the copy ON THE BOX predated
# the key it needed to look for. Nothing syncs deploy/ either. So the check below
# covers the whole deploy surface, including itself and this file.
#
# It runs BEFORE the backup and before anything is pulled, same posture as the
# APNs preflight beside it: an operator reading the refusal still has a running
# island.
if [ "$FROM_SOURCE" = "true" ]; then
  # --from-source deploys THIS CHECKOUT, so "does the box match the tag" is not
  # the question being asked and a refusal would be nonsense.
  :
elif [ "$DRIFT_CHECK" != "true" ]; then
  warn "deploy-tree drift check SKIPPED (--skip-drift-check). You are deploying an
     image built from a tag whose compose and deploy scripts may not match the ones
     on this box. That is the shape that took enspyr down on 2026-09-11. If the
     island crash-loops after the recreate, this is the first thing to check."
elif [ -f "$SCRIPT_DIR/preflight-compose-drift.sh" ]; then
  [ -x "$SCRIPT_DIR/preflight-compose-drift.sh" ] \
    || die "preflight-compose-drift.sh exists but is not executable — refusing to deploy with a disabled safety check. chmod +x it."
  # THE REF IS ASKED OF COMPOSE, NOT RE-DERIVED BESIDE IT (claude-tasks#5165).
  #
  # This used to resolve the pin with its own reader: the shell environment first
  # (Tesla, round 5: compose prefers it over .env), then .env through the dotenv
  # helper, a crude-grep cross-check in case the helper was broken (Carnot, round 8),
  # and finally the `edge` default. Every one of those steps was a second opinion
  # about what compose would interpolate, and each round found a way for the opinion
  # and the pull to disagree.
  #
  # `config --images` is not an opinion: it is the resolution `pull` and `up` use,
  # through the same island_compose (same files, same env files, same `env -u`). So
  # the ref the guard certifies and the image the deploy pulls are one value by
  # construction. The `edge` fallback is gone with the reader: an unset pin now
  # refuses in compose itself (`${ISLAND_IMAGE:?}`), so there is no silence left for
  # a cross-check to tell apart from absence.
  images="$($COMPOSE config --images)" \
    || die "compose could not resolve this island's images (see above) — nothing was changed"
  drift_ref="$(printf '%s\n' "$images" | island_ref_from_images)" \
    || die "cannot tell which release to compare this box against (see above). To
     deploy anyway, deliberately: deploy/update.sh --skip-drift-check"
  # The bare-version -> tag rewrite (`0.11.0` -> `v0.11.0`, which both live boxes
  # need) lives INSIDE the guard now, where a test can drive it — it used to sit
  # here, on the path of every real deploy, untestable (Tesla, cage-match round 2).

  # NO COMPOSE_FILE REFUSAL HERE ANY MORE, and its removal is the point rather
  # than an oversight. Round 6 added one because COMPOSE_FILE could redirect the
  # deployed file set from the environment or .env. Round 7's measurement showed
  # `-f docker-compose.yml` ignores COMPOSE_FILE entirely, so the refusal could
  # now only ever block a deploy that was going to be correct — a guard whose
  # window the pin above already closed. Subtracted, not stacked.


  set +e
  # ALL THREE SEAMS ARE STRIPPED (Carnot round 2 named the first; Tesla round 3
  # named the class). Each is an ambient variable that changes what "compared
  # against v0.11.0" MEANS, and each can arrive on a host without appearing in
  # any command line:
  #   ISLAND_REF_TREE       — substitutes a local directory for the fetched tag
  #   ISLAND_REPO_SLUG      — fetches SOMEONE ELSE'S repository
  #   ISLAND_CODELOAD_BASE  — fetches from another server entirely
  # The slug is the nastiest: inherited, the guard compares the box against a
  # stranger's tree and reports a match, while `docker compose pull` goes on
  # interpolating the real image. Fixing only the first, as an earlier pass did,
  # is patching an instance of a class that had already been named. Stripping
  # them here makes the deploy path structurally unreachable by any of them; the
  # guard separately announces any seam it finds set, for every other caller.
  env -u ISLAND_REF_TREE -u ISLAND_REPO_SLUG -u ISLAND_CODELOAD_BASE \
    "$SCRIPT_DIR/preflight-compose-drift.sh" "$REPO_ROOT" "$drift_ref"
  drift_rc=$?
  set -e
  case "$drift_rc" in
    0) ok "deploy tree matches $drift_ref" ;;
    1) die "this box's deploy tree differs from $drift_ref (diff above) — aborting BEFORE
     the backup; the island is still running. Sync the named files from the tag and
     re-run. To deploy anyway: deploy/update.sh --skip-drift-check
     EXCEPTION: rolling back BELOW v0.17.2? Do NOT sync. The older compose reads
     ISLAND_VERSION from .env and resolves 'edge' (main) once it is gone. Keep this
     compose and pass --skip-drift-check (docs/runbooks/backup-and-rollback.md)." ;;
    # NOT THE SAME ANSWER AS 'no drift'. Exit 2 means the tag could not be
    # obtained, so nothing whatsoever is known about this box's tree. Refusing is
    # the conservative read of an unknown, and the operator gets an explicit,
    # loud way through if they have checked by hand.
    *) die "could not compare this box against $drift_ref (see above) — NOTHING was
     checked, which is not the same as 'no drift found'. Fix the network or the ref,
     or deploy deliberately with: deploy/update.sh --skip-drift-check" ;;
  esac
else
  # FAIL CLOSED — and the reasoning is the OPPOSITE of the APNs preflight's
  # warn-and-continue above, which an earlier draft of this block copied without
  # re-deriving (Carnot, cage-match round 1).
  #
  # The APNs branch warns because an OLD update.sh can legitimately reach it. This
  # branch cannot: the drift check ships in the SAME commit as the code you are
  # reading, so an update.sh new enough to execute these lines came from a tag that
  # also carries preflight-compose-drift.sh. Reaching here therefore means the
  # operator synced update.sh and NOT the guard beside it — a partial sync, which
  # is the precise failure mode this PR exists to stop. Warning and continuing
  # would make the check accidentally skippable without --skip-drift-check, so the
  # guard would be defeated by exactly the behaviour it was written to refuse.
  die "deploy-tree drift check NOT FOUND ($SCRIPT_DIR/preflight-compose-drift.sh).
     This update.sh ships WITH that guard, so its absence means deploy/ was synced
     PARTIALLY — the same partial-sync that took enspyr down on 2026-09-11. Sync
     the whole of deploy/ from the tag and re-run. To deploy anyway, deliberately:
     deploy/update.sh --skip-drift-check"
fi

# --- step 1: back up the sole-copy DB (fail-closed) -------------------------
if [ "$DO_BACKUP" = "true" ]; then
  log "Step 1/3 — backing up the SQLite store (online hot copy) BEFORE any change"
  backup_dir="$REPO_ROOT/backups"; mkdir -p "$backup_dir"
  # Timestamp comes from the HOST shell (the container is slim; keep it simple).
  ts="$(date +%Y%m%d-%H%M%S)"
  # Online .backup() inside the container (no sqlite3 CLI in the slim image), then
  # copy the artifact out and remove the in-container temp. integrity_check gates.
  $COMPOSE exec -T chat-island python -c "
import sqlite3, sys
# mode=rw: the file must exist, so a missing db fails here instead of being CREATED
# and backed up as an empty file that passes integrity_check (design 18 v4 r3).
# Not mode=ro: a read-only opener cannot checkpoint a WAL db (Tesla, PR#399 r1).
try:
    src = sqlite3.connect('file:/data/aiko.db?mode=rw', uri=True)
except sqlite3.OperationalError as e:
    print('backup source /data/aiko.db is MISSING or unopenable:', e)
    sys.exit(2)
dst = sqlite3.connect('/data/_update-$ts.db')
with dst: src.backup(dst)
res = dst.execute('PRAGMA integrity_check').fetchone()[0]
print('integrity_check:', res)
sys.exit(0 if res == 'ok' else 1)
" || die "backup failed (the database is missing, or integrity_check failed: see above) — ABORTING before touching the stack"
  $COMPOSE cp "chat-island:/data/_update-$ts.db" "$backup_dir/aiko.db.preupdate-$ts" \
    || die "could not copy the backup out of the container — ABORTING"
  $COMPOSE exec -T chat-island rm -f "/data/_update-$ts.db" || true
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
  $COMPOSE --build-overlay up -d --build
else
  log "Step 2/3 — pulling the latest published image + recreating"
  $COMPOSE pull
  $COMPOSE up -d
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
die "gateway did not answer /health within ~60s after update. Inspect: deploy/compose.sh logs chat-island
     (restore path: the pre-update backup is in backups/ — see docs/runbooks/backup-and-rollback.md)"
