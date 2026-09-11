#!/usr/bin/env bash
#
# preflight-compose-drift.sh — refuse a deploy when the box's deploy tree differs
# from the tag it is about to pull. Called by update.sh before the backup;
# standalone so it can be tested directly (tests/test_deploy_compose_drift.py)
# rather than only in situ.
#
# Usage: deploy/preflight-compose-drift.sh <repo-root> <ref>
#   exit 0 — every file the box carries matches the ref (warnings may still print)
#   exit 1 — at least one file differs, or docker-compose.yml is missing; the diff
#            is on stderr
#   exit 2 — the ref could not be obtained at all, so NOTHING was learned
#
# WHY THIS EXISTS (claude-tasks#4230). update.sh pulls an IMAGE and has never
# synced docker-compose.yml — the box's copy is a separate artifact (#2301). On
# 2026-09-11 that took chat.enspyr.co down for several minutes. APNS_VOIP_TOPIC
# became a required member of config.py's all-or-none APNs group; the box's .env
# HAD the key; the box's compose did not forward it, so the value sat on the host
# where the container could never see it, and the guard refused to boot. The box's
# compose differed from the tag by EXACTLY ONE LINE. imagineering had the identical
# gap and deployed clean, because its compose happened to get synced first. Same
# change, same drift, opposite outcome, and the only variable was which files the
# operator happened to be thinking about.
#
# THE SAFETY NET THAT SHOULD HAVE CAUGHT IT WAS ITSELF DRIFTED. preflight-apns.sh
# on the box was dated Sep 1 and contained zero references to APNS_VOIP_TOPIC — the
# check written to abort before a half-set deploy could not see the key that would
# break it. Nothing syncs deploy/ either. That is why this compares the whole deploy
# surface and not just compose: a compose-only guard would still miss half of what
# happened.
#
# AN UNENFORCED SYNC GUARANTEES THE OPERATOR SYNCS THE FILES THEY ARE THINKING
# ABOUT. "Remember to sync compose" is a procedure and procedures rest on vigilance;
# this is an interlock and does not. It refuses and SHOWS rather than auto-syncing:
# a box may legitimately carry local state, and silently clobbering it is how #2301
# became a standing problem rather than a caught one. Refuse-and-show puts a human
# in the loop for one line of diff, which is the right cost. Auto-sync belongs with
# the declarative-deploy work, not here.
#
# ITS OWN BOOTSTRAP IS HONEST ABOUT ITSELF: this script lives in deploy/, which is
# the thing that does not sync. A box whose update.sh predates it will not run it at
# all, so protection begins on the deploy AFTER the one where the operator syncs
# deploy/. It does check itself thereafter — update.sh and this file are both in the
# compared set, so a box running a stale copy of either is refused by the copy it is
# running.
set -euo pipefail

c_ylw=$'\033[33m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
warn() { printf '%s warn%s %s\n' "$c_ylw" "$c_rst" "$*" >&2; }
die()  { printf '%s fail%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit "${2:-2}"; }

repo_root="${1:?usage: preflight-compose-drift.sh <repo-root> <ref>}"
ref="${2:?usage: preflight-compose-drift.sh <repo-root> <ref>}"
[ -d "$repo_root" ] || die "repo root not found: $repo_root"
repo_root="$(cd "$repo_root" && pwd)"

# Overridable so a fork, or a rename, does not silently compare against someone
# else's tree. Public repo: no credential is involved, and none should be — a
# preflight that needs a token is a preflight that fails on the box that lost one.
slug="${ISLAND_REPO_SLUG:-nickmeinhold/aiko-chat-island}"

work=""
# `return 0` IS THE WHOLE FUNCTION'S CONTRACT, not tidiness. An EXIT trap whose
# last command fails REPLACES the script's exit status in bash 3.2 — and when
# $work is empty (the ISLAND_REF_TREE path, where nothing was fetched) the test
# is false, so cleanup returned 1 and a CLEAN comparison exited 1. A guard that
# refuses every healthy deploy is worse than no guard; it gets deleted. Caught by
# the null arm, which is the arm that exists precisely because a check cannot be
# trusted to report its own success correctly just because it reports failure
# correctly.
cleanup() { [ -n "$work" ] && rm -rf "$work"; return 0; }
trap cleanup EXIT

# --- materialise the ref ----------------------------------------------------
#
# FOUR STATES, FOUR MESSAGES. "the ref does not exist", "the network refused",
# "the archive is corrupt" and "the trees match" are four different facts, and the
# only unforgivable move is to spell any of the first three the way the fourth is
# spelled. This repo has already paid once for `curl -sf` swallowing a 403 and
# being read as "no release exists", so the status code is captured and branched
# on rather than folded into curl's exit.
#
# The box is NOT a git checkout (measured on both live islands: `git rev-parse`
# answers "not a repository"). ~/apps/aiko-chat-gateway is a hand-rsynced subset,
# so `git archive` has no repository to read and the tag's bytes must come over
# the wire.
materialise_ref() {
  # ISLAND_REF_TREE is a TEST AND OFFLINE SEAM, not a bypass: it changes where the
  # ref's bytes come from and nothing else. The comparison below still runs in
  # full. An operator with a real checkout can point it at one; the tests point it
  # at a fixture. If it names a directory that is not there, that is state 1 —
  # absence — and must not degrade to "compared nothing, all good".
  if [ -n "${ISLAND_REF_TREE:-}" ]; then
    [ -d "$ISLAND_REF_TREE" ] \
      || die "ISLAND_REF_TREE is set to '$ISLAND_REF_TREE' but that directory does not exist.
     Nothing was compared. Unset it to fetch $ref from $slug instead."
    tree="$(cd "$ISLAND_REF_TREE" && pwd)"
    return 0
  fi

  command -v curl >/dev/null 2>&1 || die "missing required tool: curl (cannot fetch $ref)"
  command -v tar  >/dev/null 2>&1 || die "missing required tool: tar (cannot unpack $ref)"

  work="$(mktemp -d)"
  local tgz code path
  tgz="$work/ref.tar.gz"
  # A tag and a branch live at different paths on codeload, and asking for the
  # wrong one returns 404 — which would read as "this ref does not exist" when the
  # truth is "I looked in the wrong place". `edge` is update.sh's default and
  # tracks main, so it is translated rather than looked up as a tag.
  case "$ref" in
    edge|main) path="refs/heads/main" ;;
    *)         path="refs/tags/$ref"  ;;
  esac

  # -w for the status, NOT -f: -f makes curl exit non-zero and discard the body,
  # collapsing 403/404/500 into one unusable signal. Errors go to a file so a curl
  # failure (DNS, TLS, timeout) is reported as ERROR rather than as absence.
  if ! code="$(curl -sS -L --max-time 60 -o "$tgz" -w '%{http_code}' \
        "https://codeload.github.com/$slug/tar.gz/$path" 2>"$work/curl.err")"; then
    die "could not reach codeload.github.com to fetch $ref — the NETWORK failed, so
     nothing is known about this box's deploy tree. This is not a clean result.
     $(tr -d '\r' < "$work/curl.err" | head -3)"
  fi
  case "$code" in
    200) : ;;
    404) die "$slug has no ref '$ref' (looked at $path). Check ISLAND_VERSION in .env —
     it is the tag this deploy will pull, and a typo here means a typo there." ;;
    *)   die "codeload.github.com answered HTTP $code for $ref. Nothing was compared." ;;
  esac

  mkdir -p "$work/tree"
  # --strip-components=1: the archive's single top directory is named for the repo
  # and the ref, which is not a name anything downstream should have to know.
  tar -xzf "$tgz" -C "$work/tree" --strip-components=1 \
    || die "the archive for $ref did not unpack — it is CORRUPT or truncated, which is
     a third thing again, and still not a clean comparison."
  tree="$work/tree"
}

# CALLED DIRECTLY, NOT IN A COMMAND SUBSTITUTION, and that is load-bearing rather
# than stylistic. Inside $( ) the `die`s above would exit the SUBSHELL: the script
# would carry on with an empty tree, compare nothing, and report CLEAN — the exact
# collapse of "I could not look" into "I looked and it matched" that this file's
# exit codes exist to prevent. $work would also be set in the subshell, so the
# cleanup trap out here would never see the temp dir. It sets the global instead.
tree=""
materialise_ref

# --- compare ----------------------------------------------------------------
#
# ENUMERATION IS BOX-DRIVEN, and that is the whole reason this is not a recursive
# diff. Measured on both live islands: the box carries FOUR files under deploy/
# where the repo carries twelve. standup.sh is a first-standup tool with no reason
# to sit on a running island and deploy/secrets/ is never shipped to a box at all,
# so a whole-directory diff would refuse on every deploy of both islands, forever.
#
# Three populations, three treatments:
#   in both, differing  -> REFUSE. This is the outage class.
#   in the ref only     -> WARN. Real information (a box may now be missing
#                          something it needs) but not evidence of drift, and
#                          turning it into a refusal is what makes a guard get
#                          deleted.
#   on the box only     -> IGNORE. Both boxes are carpeted in .env.bak-* and
#                          update.sh.bak-pre-v0110; reporting them buries the one
#                          line that matters under twenty that do not.
drifted=""; drifted_n=0; compared=0

compare_one() {
  local rel="$1" box_file ref_file
  box_file="$repo_root/$rel"; ref_file="$tree/$rel"
  [ -f "$ref_file" ] || return 0          # box-only: ignored, see above
  if [ ! -f "$box_file" ]; then
    # Only reachable for a file the caller asserted must exist (compose). A file
    # the box simply does not carry never gets here — it is enumerated from the
    # ref side and warned about instead.
    drifted="$drifted $rel(MISSING-on-this-box)"; drifted_n=$((drifted_n + 1))
    return 0
  fi
  compared=$((compared + 1))
  cmp -s "$ref_file" "$box_file" && return 0
  drifted="$drifted $rel"; drifted_n=$((drifted_n + 1))
  {
    printf '\n--- %s: this box differs from %s ---\n' "$rel" "$ref"
    # -u with explicit labels: without them the header is two temp paths, and an
    # operator cannot tell which side is theirs. Truncated because a wholesale
    # divergence would otherwise scroll the actionable cases off the screen; the
    # count says how much was cut rather than leaving a silent tail.
    local total
    total="$(diff -u --label "$ref:$rel" --label "this box:$rel" "$ref_file" "$box_file" | wc -l | tr -d ' ')"
    diff -u --label "$ref:$rel" --label "this box:$rel" "$ref_file" "$box_file" | head -40
    [ "$total" -gt 40 ] && printf '    ... (%s more diff lines)\n' "$((total - 40))"
  } >&2 || true
}

# docker-compose.yml is named explicitly rather than walked, because its ABSENCE
# is a refusal where any other file's absence is a warning. It is the file the
# deploy actually runs from.
compare_one "docker-compose.yml"

# The deploy/ walk is recursive on purpose: deploy/lib/dotenv-read.sh is the single
# reader for every .env value these scripts touch, and a drifted copy of it
# mis-reads a signing seed. One level of deploy/ would not see it.
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  compare_one "$rel"
done < <(cd "$repo_root" && find deploy -type f 2>/dev/null | LC_ALL=C sort)

# Files the ref carries that this box does not. Warned, never refused.
# SCOPED TO DIRECTORIES THE BOX ACTUALLY HAS, and that is a correction made by
# running this against the real islands rather than a fixture: unscoped, it named
# 49 files on both boxes. deploy/media/, deploy/caddy/, deploy/livekit/ and
# deploy/secrets/ are separate stacks a gateway box has never carried, so listing
# them is not information, it is the wall of text an operator learns to skip —
# the same reason box-only .bak files are ignored. A file whose DIRECTORY exists
# here is a genuine "the tag added something beside what you have"; a whole
# subtree that has never been here is somebody else's deploy.
absent=""; absent_n=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  [ -d "$repo_root/$(dirname "$rel")" ] || continue
  [ -f "$repo_root/$rel" ] || { absent="$absent $rel"; absent_n=$((absent_n + 1)); }
done < <(cd "$tree" && find deploy -type f 2>/dev/null | LC_ALL=C sort)

if [ "$absent_n" -gt 0 ]; then
  warn "$ref carries $absent_n deploy file(s) this box does not — expected for a box
     that only ever needed a subset, but worth a glance if one of them is new:
    $absent"
fi

# THE COUNT IS NOT COSMETIC. Every green path above would also be green if this
# script compared nothing at all — a walk that silently found zero files reads
# exactly like a walk that found twelve matching ones. Printing the number is what
# lets the test assert the comparison was not vacuous, and lets an operator see at
# a glance that the guard was awake.
printf 'compose/deploy drift check: %s file(s) compared against %s\n' "$compared" "$ref"

if [ "$drifted_n" -gt 0 ]; then
  die "this box's deploy tree differs from $ref in $drifted_n file(s):
    $drifted
     Sync them from the tag before deploying, then re-run. Refusing BEFORE the
     backup and before anything is pulled — the island is still serving." 1
fi
exit 0
