#!/usr/bin/env bash
#
# render-config.sh — render livekit.yaml from the repo-authoritative template.
#
# Reads deploy/livekit/.env, substitutes the four per-island values into
# livekit.yaml.template, writes livekit.yaml. Fails CLOSED on anything missing:
# every failure mode of a half-rendered LiveKit config is silent at boot and
# only shows up as "calls don't connect", so this refuses rather than warns.
#
# Usage:
#   deploy/livekit/render-config.sh                 # render in place
#   deploy/livekit/render-config.sh --check         # render to stdout, write nothing
#
set -euo pipefail

c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_red=$'\033[31m'; c_rst=$'\033[0m'
log()  { printf '%s==>%s %s\n'  "$c_bold" "$c_rst" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_grn"  "$c_rst" "$*"; }
die()  { printf '%s fail%s %s\n' "$c_red" "$c_rst" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/livekit.yaml.template"
OUTPUT="$SCRIPT_DIR/livekit.yaml"
ENVFILE="$SCRIPT_DIR/.env"

CHECK_ONLY="false"
[ "${1:-}" = "--check" ] && CHECK_ONLY="true"

command -v envsubst >/dev/null 2>&1 \
  || die "envsubst not found (Debian/Ubuntu: apt-get install gettext-base)"
[ -f "$TEMPLATE" ] || die "template missing: $TEMPLATE"
[ -f "$ENVFILE" ]  || die "no $ENVFILE — copy .env.example and fill it in"

# Read the .env WITHOUT exporting the whole file into this shell's environment:
# envsubst substitutes ANY ${VAR} it can see, so a stray variable in .env whose
# name collided with template text would be silently interpolated. Only these
# four are exported, and only these four are substituted.
REQUIRED=(LIVEKIT_NODE_IP LIVEKIT_TURN_DOMAIN LIVEKIT_API_KEY_ID LIVEKIT_API_SECRET)

for var in "${REQUIRED[@]}"; do
  # Last assignment wins, matching how docker compose reads an env file.
  value="$(grep -E "^${var}=" "$ENVFILE" | tail -n1 | cut -d= -f2- || true)"
  # Strip one layer of surrounding quotes if present.
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  [ -n "$value" ] || die "$var is unset or empty in $ENVFILE — refusing to render.
     A LiveKit that boots with a blank value here fails at CONNECT, not at boot,
     which reads as 'calls are broken' rather than 'config is wrong'."
  export "$var=$value"
done

# Substitute ONLY the four names, so an unrelated ${...} in the template (or a
# future addition) cannot be silently emptied by envsubst's default all-vars mode.
subst_list="$(printf '${%s} ' "${REQUIRED[@]}")"
rendered="$(envsubst "$subst_list" < "$TEMPLATE")"

# Positive check: nothing unsubstituted survived in the EFFECTIVE config. A
# leftover ${...} means either a typo in the template or a name missing from
# REQUIRED — both produce a config LiveKit parses and then behaves wrongly on.
#
# Comments are stripped before checking, and that is not a convenience: the
# template's own prose explains the ${VAR} mechanism, so a naive whole-file scan
# fires on the documentation rather than the config. It did, on the first run.
# A check that a comment can trip is a check people learn to route around.
leftovers="$(printf '%s\n' "$rendered" | sed 's/#.*$//' | grep -n '\${' || true)"
if [ -n "$leftovers" ]; then
  printf '%s\n' "$leftovers" >&2
  die "unsubstituted \${...} remains in the config (see above) — add the name to REQUIRED"
fi

if [ "$CHECK_ONLY" = "true" ]; then
  printf '%s\n' "$rendered"
  exit 0
fi

printf '%s\n' "$rendered" > "$OUTPUT"
chmod 600 "$OUTPUT"   # contains the API secret
ok "rendered $OUTPUT (mode 600)"
