#!/usr/bin/env bash
#
# compose.sh — run `docker compose` for this island the way its deploys do.
#
# Use this instead of a bare `docker compose` on an island box:
#
#   deploy/compose.sh ps
#   deploy/compose.sh logs --tail 100 chat-island
#   deploy/compose.sh exec chat-island ls -la /data
#
# A bare `docker compose` reads only `.env`, and the image pin lives in `image.env`
# (claude-tasks#5165), so a bare call refuses with "ISLAND_IMAGE … missing a value".
# That refusal is deliberate: before #5165 the same mistake resolved `edge` and ran
# main. The mechanism and its reasons live in deploy/lib/island-compose.sh.

set -euo pipefail

# Resolve symlinks before dirname (same reason as standup.sh: a ~/bin shortcut would
# otherwise look for deploy/lib/ beside the link).
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _d="$(cd -P "$(dirname "$_src")" && pwd)"; _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_d/$_src" ;; esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$_src")" && pwd)"
ISLAND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/island-compose.sh
. "$SCRIPT_DIR/lib/island-compose.sh"

# The pin first: a missing image.env refuses before docker is touched at all.
island_refuse_global_options "$@" || exit 1
island_require_pin || exit 1
island_docker_detect || exit 1
island_compose "$@"
