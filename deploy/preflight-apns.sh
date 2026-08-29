#!/usr/bin/env bash
#
# preflight-apns.sh — refuse a deploy that would crash-loop the box on a PARTIAL
# APNs credential set. Called by update.sh before the backup; standalone so it can
# be tested directly (tests/test_deploy_preflight.py) rather than only in situ.
#
# Usage: deploy/preflight-apns.sh <path-to-.env>
#   exit 0 — all four APNS_* set, or none (both are fine)
#   exit 1 — partially configured; message on stderr names which
#   exit 0 — .env absent (nothing to check; standup handles a missing .env)
#
# WHY THIS EXISTS (claude-tasks#3366, cage-match PR#141 round 3, Tesla — an accepted
# risk recorded rather than absorbed). APNS_* used to be dead ink on a host: compose
# did not forward it, so a half-drafted credential set sat harmlessly in .env and the
# island booted with push off. PR#141 made compose forwarding total, so those bytes
# now reach the container, where config.py's half-configured guard deliberately
# REFUSES TO BOOT.
#
# The guard is correct and must not be weakened — a partial set reads as "push is on"
# at every call site while every send fails at Apple's door, and on a handset a missed
# call is indistinguishable from a disabled feature. What changed is WHEN it fails: at
# boot instead of never. With `restart: always` that is a crash-loop on a box nobody
# edited, triggered by a version bump. Catching it here means the operator reads this
# message while their island is still running.
set -euo pipefail

env_file="${1:?usage: preflight-apns.sh <path-to-.env>}"
[ -f "$env_file" ] || exit 0

# PARSER SKEW IS THE REAL HAZARD HERE, and it is bidirectional (cage-match PR#148,
# Carnot MEDIUM — verified, and worse than reported). This script is a SECOND reader of
# a file whose FIRST reader is python-dotenv, via pydantic-settings. Any form dotenv
# accepts and this misses makes the two disagree:
#
#   * all four written `export APNS_...=x` -> island sees four and boots; a naive
#     `^KEY=` match sees ZERO and reports "none configured". Silent pass.
#   * three plain, one `export` -> island sees four and boots; naive match sees three
#     and ABORTS A HEALTHY DEPLOY. That false red is the worse direction: it blocks a
#     box that was fine, and a safety check that cries wolf gets deleted.
#
# Measured against python-dotenv on a fixture: it read three keys where `^KEY=` read
# one. So this matches dotenv's real grammar — optional leading whitespace, optional
# `export `, optional spaces around `=`, optional surrounding quotes.
#
# RESIDUAL SKEW, named rather than pretended away: multi-line quoted values and
# variable interpolation (`${OTHER}`) are NOT handled. Neither appears in either live
# box's .env, and both would need a real parser rather than a regex. If a box ever
# needs one, exec the check inside the running container and use the island's OWN
# dotenv rather than growing a third parser here.
set_keys=(); missing_keys=()
for k in APNS_KEY_ID APNS_TEAM_ID APNS_TOPIC APNS_PRIVATE_KEY; do
  # Present AND non-blank. config.py restores absence for a whitespace-only value
  # (claude-tasks#3358), so a key with a blank value is "unset" to the island and must
  # not count as partially-configured here either — otherwise this preflight would
  # abort a deploy on a box that boots perfectly well.
  value=$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${k}[[:space:]]*=[[:space:]]*//p" \
            "$env_file" | tail -1)
  # Strip one layer of matching quotes the way dotenv does, THEN test for blank — so
  # APNS_TOPIC="" and APNS_TOPIC="   " both read as unset, matching the island.
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  value=$(printf '%s' "$value" | tr -d '[:space:]')
  if [ -n "$value" ]; then set_keys+=("$k"); else missing_keys+=("$k"); fi
done

if [ "${#set_keys[@]}" -gt 0 ] && [ "${#missing_keys[@]}" -gt 0 ]; then
  printf 'APNs is HALF-CONFIGURED in %s\n' "$env_file" >&2
  printf '  set:             %s\n' "${set_keys[*]}" >&2
  printf '  missing or blank: %s\n' "${missing_keys[*]}" >&2
  printf 'The island REFUSES TO BOOT on a partial set, so this deploy would crash-loop\n' >&2
  printf 'the box. Set ALL four or NONE, then re-run. Aborting before any change.\n' >&2
  exit 1
fi
exit 0
