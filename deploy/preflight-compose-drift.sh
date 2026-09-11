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

# NORMALISE HERE, NOT AT THE CALL SITE (Tesla, cage-match round 2). Both live
# boxes' .env reads `ISLAND_VERSION=0.11.0` while the git tag is `v0.11.0` — the
# image registry accepts the bare form, git does not. That rewrite is on the path
# of EVERY real deploy, and inline in update.sh it could not be driven by a test:
# delete the arm and every deploy 404s into "could not look", or worse, a later
# "helpful" change turns 404 into "empty tree" and it reads CLEAN. One place, one
# test. `edge` and `main` are branch names and pass through untouched.
case "$ref" in [0-9]*) ref="v$ref" ;; esac

# THE THREE SEAMS, HANDLED AS ONE CLASS (Tesla, round 3). Each of these changes
# what "compared against $ref" MEANS, and each is an ambient environment variable
# — so each can arrive on a production host without appearing in any command line.
# An earlier pass fixed exactly one of them (ISLAND_REF_TREE) and left its
# siblings live, which is patching an instance where the class was already named:
# ISLAND_REPO_SLUG inherited from a host fetches SOMEONE ELSE'S tarball, and if
# that tree happens to match the box the guard reports the trees match while
# `docker compose pull` still interpolates the real image.
#
# Two enforcements, because either alone is insufficient: update.sh strips ALL
# THREE before invoking, so the deploy path cannot reach them; and every one of
# them announces itself here, so any other caller is told what it is really
# comparing. A seam that can be silent is not a seam, it is a trapdoor.
slug="${ISLAND_REPO_SLUG:-nickmeinhold/aiko-chat-island}"
codeload="${ISLAND_CODELOAD_BASE:-https://codeload.github.com}"
for _seam in ISLAND_REF_TREE ISLAND_REPO_SLUG ISLAND_CODELOAD_BASE; do
  eval "_v=\${$_seam:-}"
  [ -n "$_v" ] || continue
  warn "$_seam is set ('$_v') — this run does NOT mean what an unconfigured run
     means. A clean result below says nothing about the tag this box would deploy."
done

work=""
work_list="$(mktemp)"
# `return 0` IS THE WHOLE FUNCTION'S CONTRACT, not tidiness. An EXIT trap whose
# last command fails REPLACES the script's exit status in bash 3.2 — and when
# $work is empty (the ISLAND_REF_TREE path, where nothing was fetched) the test
# is false, so cleanup returned 1 and a CLEAN comparison exited 1. A guard that
# refuses every healthy deploy is worse than no guard; it gets deleted. Caught by
# the null arm, which is the arm that exists precisely because a check cannot be
# trusted to report its own success correctly just because it reports failure
# correctly.
cleanup() { [ -n "$work" ] && rm -rf "$work"; rm -f "$work_list"; return 0; }
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
    # IT MUST ANNOUNCE ITSELF (Carnot, round 2). An ambient variable that silently
    # redefines what the check MEANS is the worst possible shape for this seam: the
    # run still prints "N file(s) compared against v0.11.0" while having compared
    # against whatever an inherited variable pointed at. That is the difference
    # between "I compared this box to the tag" and "I compared it to something" —
    # the exact epistemic collapse the exit codes exist to prevent, smuggled in
    # through the environment instead of through a code path. update.sh strips the
    # variable before invoking, so the deploy path cannot reach here at all; this
    # warning is for every other caller.
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
  # --proto/--proto-redir '=https': `-L` alone will happily follow a redirect from
  # https to PLAIN HTTP, which turns a TLS-protected fetch into an unauthenticated
  # one at the exact moment its bytes decide whether a production island deploys.
  # --max-redirs bounds the chase; --max-filesize bounds the body, because the
  # next thing that happens to it is tar. (Tesla, round 2. Its companion claim —
  # path traversal through `--strip-components` — is REFUTED by measurement: a
  # member named `top/../../../escaped.txt` is refused by bsdtar AND GNU tar, and
  # both exit non-zero, so the `|| die` below already fires. Measured on macOS 15
  # and on the live Ubuntu box rather than reasoned about.)
  # THE PROTOCOL PIN IS SCOPED TO THE DEFAULT HOST, and that scoping is the
  # answer to a real tension rather than a convenience. `--proto '=https'` must
  # hold absolutely on the path a production island takes — it is what stops `-L`
  # following a redirect down to plain HTTP and turning a TLS-protected fetch
  # into an unauthenticated one at the moment its bytes decide whether an island
  # deploys. But it also, correctly, refuses a `http://127.0.0.1:PORT` base — and
  # that base is the only way to drive the corrupt-archive and non-404 HTTP arms
  # without the network, which are branches that otherwise have no test at all.
  #
  # So: unoverridden, https is mandatory. Overridden, the operator's scheme is
  # honoured — and that override is already announced by the seam loop above and
  # already stripped by update.sh, so it cannot reach a deploy. Weakening the
  # default to make a test pass would have been the wrong trade; scoping it is
  # not the same move.
  proto_args=""
  [ -z "${ISLAND_CODELOAD_BASE:-}" ] && proto_args="--proto =https --proto-redir =https"
  # shellcheck disable=SC2086 -- proto_args is a controlled two-flag literal set here,
  # never user input; quoting it would pass one empty argument in the default case.
  if ! code="$(curl -sS -L $proto_args --max-redirs 3 \
        --max-filesize 100000000 --max-time 60 -o "$tgz" -w '%{http_code}' \
        "$codeload/$slug/tar.gz/$path" 2>"$work/curl.err")"; then
    die "could not reach $codeload to fetch $ref — the NETWORK failed, so
     nothing is known about this box's deploy tree. This is not a clean result.
     $(tr -d '\r' < "$work/curl.err" | head -3)"
  fi
  case "$code" in
    200) : ;;
    404) die "$slug has no ref '$ref' (looked at $path). Check ISLAND_VERSION in .env —
     it is the tag this deploy will pull, and a typo here means a typo there." ;;
    *)   die "$codeload answered HTTP $code for $ref. Nothing was compared." ;;
  esac

  mkdir -p "$work/tree"
  # --strip-components=1: the archive's single top directory is named for the repo
  # and the ref, which is not a name anything downstream should have to know.
  # --no-same-owner: never let archive metadata pick the uid on a host where this
  # may run under sudo. (Default for non-root, explicit because the deploy user is
  # not always the one you think.)
  tar -xzf "$tgz" -C "$work/tree" --no-same-owner --strip-components=1 \
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

# THE REF TREE MUST CONTAIN THE FILE THIS CHECK IS NAMED FOR. Without this, an
# empty or wrong-shaped tree (a tarball that unpacks to nothing, a
# --strip-components that ate the whole thing, an ISLAND_REF_TREE pointed at an
# unrelated directory) makes every compare_one() take its `[ -f "$ref_file" ] ||
# return 0` exit, compare NOTHING, and exit 0 — "I could not look" wearing
# "clean"'s clothes for the second time in one file. Found in self-review by
# asking what the count is EVIDENCE of: it was printed and never acted on, which
# is what an unenforced invariant looks like.
[ -f "$tree/docker-compose.yml" ] \
  || die "the tree for $ref has no docker-compose.yml at its root — the ref was not
     materialised correctly (empty archive, wrong ISLAND_REF_TREE, unexpected
     layout). NOTHING was compared, and that is not a clean result."
# AND IT MUST HAVE deploy/ (Tesla, round 3). A ref tree holding a matching compose
# and nothing else is well-formed enough to pass the check above, and then every
# script the box carries is "a file the ref does not have" — box-only, ignored —
# so a drifted update.sh reads as CLEAN. The compose check alone certifies one
# file; this certifies the SHAPE. GitHub's real archive always has both, which is
# exactly why the happy path hides it.
[ -d "$tree/deploy" ] \
  || die "the tree for $ref has no deploy/ directory. Every script on this box would
     be treated as box-only and skipped, so a clean result would mean nothing.
     The ref did not materialise as this repository."
# The mirror: a box with no deploy/ at all cannot be compared meaningfully either,
# and saying so beats silently comparing one file.
[ -d "$repo_root/deploy" ] \
  || die "this box has no deploy/ directory, but $ref does. Nothing here matches the
     shape of an island deploy tree." 1
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
# PRESENCE, DEFINED ONCE (cage-match round 5, Carnot — and the THIRD round in
# which a fix landed on one instance of a class already named). `-e` follows a
# symlink, so a DANGLING link reads as "not there". That is the wrong answer
# everywhere in this script: a deploy file or a compose fragment that resolves to
# nothing is a real artifact in a broken state, and calling it absent downgrades
# a refusal to a warning. The deploy walk had `-e || -L`; the root-compose walk
# and the override loop still had bare `-e`, so the identical shape produced
# DRIFT in one place and CLEAN in another.
#
# Every box-side presence test now goes through here, which is what makes the
# answer uniform by construction instead of by three people remembering.
_present() { [ -e "$1" ] || [ -L "$1" ]; }

drifted=""; drifted_n=0; compared=0
# Declared up here, not beside the ref-side walk, because the root-compose walk
# above also contributes to it now.
absent=""; absent_n=0; absent_rollup=""

compare_one() {
  local rel="$1" box_file ref_file
  box_file="$repo_root/$rel"; ref_file="$tree/$rel"
  [ -f "$ref_file" ] || return 0          # box-only: ignored, see above
  if [ ! -f "$box_file" ]; then
    # Two ways in. (1) docker-compose.yml, named explicitly below, whose absence
    # is a refusal rather than a warning. (2) a BROKEN SYMLINK: the walk now
    # enumerates -type l, and `-f` follows the link, so a deploy file pointing at
    # nothing lands here and is refused — which is the right answer for it. A file
    # the box simply does not carry never reaches this branch; it is enumerated
    # from the ref side and warned about instead.
    drifted="$drifted $rel(MISSING-on-this-box)"; drifted_n=$((drifted_n + 1))
    return 0
  fi
  # UNREADABLE IS NOT "DIFFERENT" (cage-match round 5, Carnot). Without this,
  # a file the deploy user cannot read passes `-f`, fails `cmp`, and is reported
  # as ordinary DRIFT with an empty diff body — so update.sh tells the operator
  # to sync a file that is already identical, and the real fault (permissions) is
  # never named. Measured before the fix: exit 1, "this box differs", no diff.
  # It is the four-state contract again: I-could-not-read is a measurement
  # failure, not an observation.
  [ -r "$box_file" ] || die "cannot READ $box_file (permissions?). That is not the same
     as finding a difference — nothing was learned about this file, so nothing is
     being claimed about it."
  [ -r "$ref_file" ] || die "cannot READ $ref_file inside the $ref tree. Nothing was
     learned about this file."
  compared=$((compared + 1))
  cmp -s "$ref_file" "$box_file" && return 0
  drifted="$drifted $rel"; drifted_n=$((drifted_n + 1))
  # THE DIFF IS CAPTURED ONCE, WITH ITS STATUS HANDLED EXPLICITLY — and this is a
  # concession to a reviewer who was wrong three times about the same lines, on a
  # point that was nonetheless worth conceding.
  #
  # The previous shape relied on `{ ... } >&2 || true` to suppress errexit for a
  # `total="$(diff | wc -l)"` assignment, because `diff` exits 1 on a difference
  # and `pipefail` propagates it. That WORKS — driven on bash 3.2/macOS and bash
  # 5/Linux, on a 3-line and a 60-line divergence, the diff prints, the truncation
  # notice prints, the refusal prints. Carnot called it a live bug in rounds 2, 3
  # and 4 and it was refuted with a run each time.
  #
  # But the third raising is a fact about the CODE, not only about the reviewer:
  # the correctness of the red path — the one path this whole file exists to
  # execute — rested on a subtle rule about where errexit is disabled, three
  # levels of nesting from the thing it protects. An editor deleting a `|| true`
  # that looks decorative would break the refusal and every green test would stay
  # green. Explicit `set +e` around one capture says what it means, needs no rule
  # to read, and runs `diff` once instead of twice.
  local total diff_out
  set +e
  diff_out="$(diff -u --label "$ref:$rel" --label "this box:$rel" "$ref_file" "$box_file")"
  set -e
  total=$(printf '%s\n' "$diff_out" | wc -l | tr -d ' ')
  {
    printf '\n--- %s: this box differs from %s ---\n' "$rel" "$ref"
    # Labels are not cosmetic: without them the header is two temp paths and the
    # operator cannot tell which side is theirs. Truncated because a wholesale
    # divergence would scroll the actionable cases off the screen; the count says
    # how much was cut rather than leaving a silent tail.
    printf '%s\n' "$diff_out" | head -40
    [ "$total" -gt 40 ] && printf '    ... (%s more diff lines)\n' "$((total - 40))"
  } >&2 || true
}

# docker-compose.yml is named explicitly rather than walked, because its ABSENCE
# is a refusal where any other file's absence is a warning. It is the file the
# deploy actually runs from.
compare_one "docker-compose.yml"

# EVERY ROOT-LEVEL COMPOSE FILE, not just the one — "the compose file" is a
# convenient singular that docker does not share. Any other `docker-compose*.yml`
# / `compose*.yml` the ref carries is compared on the same terms as a deploy file.
while IFS= read -r -d '' rel; do
  rel="${rel#./}"
  [ -n "$rel" ] || continue
  [ "$rel" = "docker-compose.yml" ] && continue
  # BOX-DRIVEN, like every other walk here — and this line is the whole fix for a
  # regression that reached the live boxes before any test saw it. `compare_one`
  # treats a missing box file as DRIFT, which is right for docker-compose.yml (it
  # is the file the deploy runs from) and wrong for every other root compose
  # fragment: the repo ships `docker-compose.build.yml`, no island carries it
  # because islands never build, and refusing on its absence made BOTH clean
  # production boxes exit 1. A guard that refuses every healthy deploy is the one
  # outcome that cannot ship, and 33 green unit tests said nothing, because no
  # fixture had a ref-only root compose file. Absent here is a warning, same as
  # any other deploy file the box legitimately does not need.
  if ! _present "$repo_root/$rel"; then
    absent="$absent $rel"; absent_n=$((absent_n + 1))
    continue
  fi
  compare_one "$rel"
  # `${rel#./}` rather than `sed -z`, and this one is not a style call. BSD sed
  # HAS NO -z (measured: macOS sed errors, GNU sed on the island is fine), so the
  # pipeline died on the orchestrator and the walk silently yielded nothing —
  # a GNU-only flag turning a comparison into a no-op, which is the exact class
  # Carnot warned about for `sort -z`. That warning was wrong about sort (Apple's
  # sort does support -z, measured twice) and right about the class, and I put a
  # real instance of it one line away while rejecting the false one. Shell
  # parameter expansion is portable and needs no process.
done < <(cd "$tree" && find . -maxdepth 1 \( -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' \
           -o -name 'compose*.yml' -o -name 'compose*.yaml' \) -print0 2>/dev/null \
         | LC_ALL=C sort -z)

# THE ONE BOX-ONLY FILE THAT IS NOT IGNORABLE, and the only exception in this
# script to "a file the ref does not carry is none of our business".
#
# MEASURED on the live box, not read in a doc: `docker compose config` with no
# `-f` flags merged a `docker-compose.override.yml` sitting beside the base file
# and emitted its variables into the resolved config. `update.sh`'s deploy path is
# exactly that — a bare `docker compose pull && up`, no `-f` — so an override the
# tag has never heard of is silently part of what gets deployed.
#
# That is this guard's own subject matter: compose on the box differing from
# compose in the tag, changing what the container sees. It arrives as an EXTRA
# file rather than an edited one, which is precisely why the box-only rule would
# have waved it through. Neither island carries one today (checked); the point is
# that one appearing is invisible to every other check here.
for ovr in compose.override.yaml compose.override.yml \
           docker-compose.override.yaml docker-compose.override.yml; do
  _present "$repo_root/$ovr" || continue
  [ -f "$tree/$ovr" ] && continue   # the ref carries it too — already compared above
  drifted="$drifted $ovr(BOX-ONLY-but-AUTO-LOADED)"; drifted_n=$((drifted_n + 1))
  warn "$ovr exists on this box and NOT in $ref. docker compose auto-loads an
     override file when no -f flags are given, which is how update.sh deploys — so
     this file is silently part of the running config and no tag can account for
     it. Every other box-only file is ignored; this one cannot be."
done

# The deploy/ walk is recursive on purpose: deploy/lib/dotenv-read.sh is the single
# reader for every .env value these scripts touch, and a drifted copy of it
# mis-reads a signing seed. One level of deploy/ would not see it.
# `-o -type l` (Maxwell + Carnot, converging independently): plain `-type f`
# EXCLUDES symlinks, so a box with `deploy/update.sh -> /opt/island/update.sh` —
# a perfectly plausible shape for a hand-built deploy tree — drops that file from
# the compared set SILENTLY and its drift becomes invisible. A hole that reports
# as a clean pass, in exactly the population of boxes most likely to have a
# hand-rolled layout. `[ -f ]` and `cmp` both follow the link, so a live symlink
# compares against its target; a BROKEN one fails `[ -f ]` and is reported as
# drift, which is the right answer for a deploy file pointing at nothing.
#
# `-L` (Carnot, round 2): the round-1 symlink fix covered symlinked FILES and not
# symlinked DIRECTORIES, which is the worse half. `deploy/lib -> /opt/island/lib`
# is listed by `-type l` and never TRAVERSED, so `deploy/lib/dotenv-read.sh` — the
# single reader for every .env value these scripts touch — is neither compared nor
# warned about, because the ref-side absent check follows the link and sees it
# present. Measured: without -L the walk yields `deploy/lib`; with -L it yields
# `deploy/lib/dotenv-read.sh`. A fix that closed the narrow case and left the wide
# one open is worse than none, because it reads as done.
#
# -print0 / read -d '' (Kelvin): a newline in a filename splits one path into two
# in a line-oriented read, and the halves silently miss their comparison. Nothing
# in this repo carries one today, which is the same "today" that produced the
# outage this file exists to refuse.
# THE WALK'S EXIT STATUS IS CHECKED (Tesla, round 3). In `done < <(find ...)` the
# process substitution's status is discarded: if find is missing, or deploy/ is
# unreadable, the stream is simply EMPTY — no files compared, nothing drifted,
# compose still matches, exit 0. A failed walk was indistinguishable from a clean
# one, which is this file's signature failure committed by the walk that looks
# for it. Staged to a file so the status is the terminal command's own.
box_list="$work_list"
( cd "$repo_root" && find -L deploy \( -type f -o -type l \) -print0 ) > "$box_list" 2>/dev/null \
  || die "could not walk deploy/ on this box (find failed). Nothing was compared."
while IFS= read -r -d '' rel; do
  [ -n "$rel" ] || continue
  compare_one "$rel"
done < <(LC_ALL=C sort -z "$box_list")

# Files the ref carries that this box does not. Warned, never refused.
# REPORTED IN FULL, BUT GROUPED — a correction made twice, each time by running
# this against the real islands rather than a fixture.
#
# First pass listed every absent file: 49 of them on both boxes, because
# deploy/media/, deploy/caddy/, deploy/livekit/ and deploy/secrets/ are separate
# stacks a gateway box has never carried. That is not information, it is the wall
# of text an operator learns to skip.
#
# Second pass suppressed any file whose DIRECTORY is absent here — which cut it to
# 4, and opened a new hole Tesla named in round 2: a tag adding
# `deploy/hooks/new-guard.sh` creates a directory the box has never had, so the
# one genuinely new thing would be the one thing silently dropped. Filtering by
# what the box already knows about cannot report what the box has never seen.
#
# So nothing is dropped now. Files in a subtree the box lacks entirely are rolled
# up to one line per subtree with a count; files beside something the box already
# has are named individually, because those are the ones worth reading. Full
# information, four lines instead of forty-nine.
( cd "$tree" && find -L deploy \( -type f -o -type l \) -print0 ) > "$box_list" 2>/dev/null \
  || die "could not walk deploy/ in the $ref tree (find failed). Nothing was compared."
while IFS= read -r -d '' rel; do
  [ -n "$rel" ] || continue
  if [ ! -d "$repo_root/$(dirname "$rel")" ]; then
    # A subtree this box has never carried. Counted and rolled up, never dropped.
    absent_rollup="$absent_rollup
$(printf '%s' "$rel" | cut -d/ -f1-2)"
    continue
  fi
  # `-e || -L`, from the fix-interaction pass rather than from a failing test.
  # `-e` follows the link, so a BROKEN symlink reads as absent here — while the
  # compare walk above now enumerates `-type l` and reports that same file as
  # DRIFT. One file, two contradictory sentences in one run: "your box does not
  # carry this" and "your copy of this differs". `-L` catches the dangling link
  # as present, leaving exactly one report, the refusal, which is the true one.
  _present "$repo_root/$rel" \
    || { absent="$absent $rel"; absent_n=$((absent_n + 1)); }
done < <(LC_ALL=C sort -z "$box_list")

if [ "$absent_n" -gt 0 ]; then
  warn "$ref carries $absent_n deploy file(s) BESIDE ones this box has — these sit in
     directories the box already uses, so a new one is worth a look:
    $absent"
fi
if [ -n "$absent_rollup" ]; then
  # One line per never-carried subtree with its count. The rollup is what keeps
  # this honest without being unreadable: a brand-new `deploy/hooks/` shows up as
  # its own line the first time it appears, rather than being filtered out for the
  # crime of being new.
  rollup_lines="$(printf '%s' "$absent_rollup" | grep -v '^$' | LC_ALL=C sort | uniq -c \
    | awk '{printf "     %s/ (%s file(s))\n", $2, $1}')"
  warn "$ref also carries these deploy SUBTREES this box has never had (normal for a
     gateway box — media/caddy/livekit/secrets belong to other stacks — but a name
     you do not recognise here is new):
$rollup_lines"
fi

# THE COUNT IS NOT COSMETIC. Every green path above would also be green if this
# script compared nothing at all — a walk that silently found zero files reads
# exactly like a walk that found twelve matching ones. Printing the number is what
# lets the test assert the comparison was not vacuous, and lets an operator see at
# a glance that the guard was awake.
printf 'compose/deploy drift check: %s file(s) compared against %s\n' "$compared" "$ref"

# NO `compared -gt 0` FLOOR HERE, and its absence is the considered answer rather
# than an omission. One was added as a belt on the vacuous-comparison fix and then
# mutation-tested: deleting it changed no test result, which is the signature of a
# check whose disabled value equals its enabled value. Chasing why rather than
# writing an arm for it: `compared` is 0 only if docker-compose.yml failed to
# increment it, the ref-side copy is guaranteed present by the check above, so the
# box's copy must have been missing — and that already pushed `drifted_n` to 1.
# The floor was therefore unreachable except to REPLACE a truthful drift refusal
# with a vaguer "could not look". The invariant it was groping for is enforced one
# screen up, where it is proven by an arm that actually goes red.
if [ "$drifted_n" -gt 0 ]; then
  die "this box's deploy tree differs from $ref in $drifted_n file(s):
    $drifted
     Sync them from the tag before deploying, then re-run. Refusing BEFORE the
     backup and before anything is pulled — the island is still serving." 1
fi
exit 0
