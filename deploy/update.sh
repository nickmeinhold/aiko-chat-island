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
COMPOSE="$DOCKER compose -f docker-compose.yml"

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
  # The ref to compare against is the one compose will actually interpolate, read
  # through the single dotenv reader rather than a fifth grep (deploy/lib). An
  # unset ISLAND_VERSION means `edge`, which is this script's own documented
  # default and tracks main.
  # shellcheck source=lib/dotenv-read.sh
  . "$SCRIPT_DIR/lib/dotenv-read.sh"
  # THE SELECTOR MUST RESOLVE THE WAY COMPOSE RESOLVES IT (Tesla, cage-match
  # round 5). Measured on the live box: `docker compose config` prefers the SHELL
  # ENVIRONMENT over `.env` — an exported TAGVAR beat the .env value outright.
  # Reading only `.env`, as this did, means an exported ISLAND_VERSION (direnv, a
  # systemd `Environment=`, a leftover export, or this script's own documented
  # `ISLAND_VERSION=v0.1.0 deploy/update.sh` pin path) makes the guard fetch and
  # bless one tag while `docker compose pull` interpolates a DIFFERENT one.
  #
  # That is the 2026-09-11 outage wearing the interlock's own clothes: new image,
  # old compose, a printed clean comparison, and an island that serves until it
  # does not. The comment here used to claim this was "the ref compose will
  # actually interpolate" — it was not, which is prose overclaiming the code
  # inside the guard written to stop prose from governing deploys.
  #
  # And note which class this belongs to: ISLAND_VERSION is the PRODUCTION member
  # of the ambient-input family whose three TEST members were swept two rounds
  # ago. The class was named and one instance was left live.
  drift_ref="${ISLAND_VERSION:-}"
  [ -n "$drift_ref" ] || drift_ref="$(dotenv_read "$REPO_ROOT/.env" ISLAND_VERSION)"

  # A POSITIVE CONTROL ON THE HELPER, WITH A DIFFERENT INSTRUMENT (Carnot,
  # cage-match round 8), and the reason it exists is that my first answer to the
  # finding was wrong.
  #
  # The circularity Carnot named is real: this resolves the ref by sourcing
  # deploy/lib/dotenv-read.sh, a file INSIDE the surface the guard is about to
  # certify. I assumed the failure direction was closed — a broken helper returns
  # empty, drift_ref falls back to `edge`, the box gets compared against main and
  # refuses. MEASURED on the live box, that assumption is FALSE: box-vs-main
  # exits 0 today, because main's deploy tree happens to equal v0.11.0's. So a
  # broken helper would have the guard print a clean comparison for a ref it is
  # NOT deploying, silently — exactly the epistemic collapse the exit codes exist
  # to prevent, arriving through the bootstrap instead of through a code path.
  #
  # The check is a DELIBERATELY CRUDE grep, not a second parser. It shares no
  # grammar with the helper, so it fails differently: it answers only "does .env
  # mention this key at all", and a disagreement between the two means the helper
  # is broken rather than the key absent. Absence is legitimate (an unpinned box
  # tracks `edge`); a key that is visibly THERE and unreadable is not.
  if [ -z "$drift_ref" ] && [ -f "$REPO_ROOT/.env" ] \
     && LC_ALL=C grep -aqE '^[[:space:]]*(export[[:space:]]+)?ISLAND_VERSION[[:space:]]*=' "$REPO_ROOT/.env"; then
    die "'.env' contains an ISLAND_VERSION line but the dotenv helper read nothing
     from it — deploy/lib/dotenv-read.sh is broken or stale. Falling back to 'edge'
     here would compare this box against main and could print CLEAN for a ref this
     deploy will not use. Refusing instead. Refresh deploy/ from the tag."
  fi
  [ -n "$drift_ref" ] || drift_ref="edge"
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
     re-run. To deploy anyway: deploy/update.sh --skip-drift-check" ;;
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
src = sqlite3.connect('/data/aiko.db')
dst = sqlite3.connect('/data/_update-$ts.db')
with dst: src.backup(dst)
res = dst.execute('PRAGMA integrity_check').fetchone()[0]
print('integrity_check:', res)
sys.exit(0 if res == 'ok' else 1)
" || die "backup integrity_check failed — ABORTING before touching the stack"
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
  $DOCKER compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
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
die "gateway did not answer /health within ~60s after update. Inspect: docker compose logs chat-island
     (restore path: the pre-update backup is in backups/ — see docs/deploy-passkeys-runbook.md)"
