#!/usr/bin/env bash
# Resolve the gateway .env values that are OPERATOR CHOICES, so a standup re-run
# preserves them instead of silently reverting them to a default.
#
# WHY THIS FILE EXISTS. PR#151 found a class in standup.sh's media wiring and closed
# it there: "an operator CHOICE must be recorded and read back; a measured FACT should
# be re-derived." The class pass was then scoped to one code path. Testing that claim
# (#3734) found GATEWAY_SEED_PEERS with the same hole in the same file, and a
# systematic sweep of the .env heredoc found a second, PASSKEY_ENABLED. This is the
# sibling of resolve-media-env.sh for the non-media half.
#
# WHAT MAKES THESE TWO WORSE THAN THE HOSTNAME BUG. Both fail SILENTLY and look
# healthy:
#   * GATEWAY_SEED_PEERS is the federation link. Each live island's seed list is the
#     other island. Reset to [], peers_service just serves self — no fetch, no error,
#     no log line. The islands simply stop knowing about each other.
#   * PASSKEY_ENABLED is the PRIMARY sign-in ingress since social was dropped
#     (#1923). Reset to false, /v1/auth/providers stops advertising passkeys and
#     nobody can sign in, on an island whose health check is green.
#
# NOT IN THE CLASS, checked rather than assumed:
#   * JWT_SECRET       — already read back and reused (standup.sh, "not rotated").
#   * GATEWAY_BASE_URL / GATEWAY_DISPLAY_NAME — fail CLOSED: required, prompted, or
#     `die`. A re-run cannot silently blank them.
#   * LIVEKIT_DOMAIN / LIVEKIT_TURN_DOMAIN    — resolve-media-env.sh owns these.
#   * LIVEKIT_NODE_IP  — a measured FACT about the host. Deliberately re-derived every
#     run, which is correct when a box's public IP changes.
#
# Usage:
#   resolve-gateway-env.sh <gateway-env> [seed-peers-flag] [passkeys-flag]
#
# passkeys-flag is one of "" (no flag given) | "true" (--enable-passkeys) |
# "false" (--no-passkeys). The empty string is NOT the same as "false": empty means
# "the operator said nothing this run", which is exactly the case that must consult
# the record. Collapsing those two is the bug this script exists to prevent.
#
# Prints on stdout (KEY=VALUE, one per line):
#   SEED_PEERS=<json array>
#   PASSKEY_ENABLED=true|false
set -euo pipefail

gw_env="${1:?usage: resolve-gateway-env.sh <gateway-env> [seed-peers-flag] [passkeys-flag]}"
seed_flag="${2:-}"
passkeys_flag="${3:-}"

# Read the LAST assignment of a key, matching how python-dotenv resolves duplicates.
_read_kv() { [ -f "$1" ] && grep -E "^$2=" "$1" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

# RESOLUTION ORDER, identical for both: flag, then the value this island RECORDED,
# then convention. The middle rung is the one that was missing.
seed_peers="$seed_flag"
[ -n "$seed_peers" ] || seed_peers="$(_read_kv "$gw_env" GATEWAY_SEED_PEERS)"
[ -n "$seed_peers" ] || seed_peers="[]"

# Same order, but the flag is TRI-STATE. --enable-passkeys and --no-passkeys are both
# explicit operator statements and both win; absence consults the record. Without
# --no-passkeys there would be no way to turn passkeys off once read-back exists, and
# "false" would be ambiguous between "operator chose off" and "never set".
passkey_enabled="$passkeys_flag"
[ -n "$passkey_enabled" ] || passkey_enabled="$(_read_kv "$gw_env" PASSKEY_ENABLED)"
[ -n "$passkey_enabled" ] || passkey_enabled="false"

# SINGLE-LINE GUARANTEE, enforced HERE rather than trusted of every caller. Output
# is a newline-delimited KEY=VALUE stream, so a value containing a newline would be
# read back as a truncated first line — `GATEWAY_SEED_PEERS=[` silently replacing the
# federation list, which is the very failure this script exists to prevent. The guide
# documents a MULTILINE --seed-peers array (docs/standup-guide.md), so this is the
# documented path, not a hostile edge. Newlines are insignificant whitespace outside a
# JSON string (a literal newline inside one must be escaped as \n), so collapsing them
# is safe. A `.env` is line-oriented too, so a multiline value could never round-trip
# anyway — normalising makes the documented invocation work for the first time.
seed_peers="$(printf '%s' "$seed_peers" | tr '\n\r' '  ')"

case "$passkey_enabled" in
  true|false) ;;
  *) echo "resolve-gateway-env: PASSKEY_ENABLED must be true or false, got '$passkey_enabled'" >&2; exit 1 ;;
esac

printf 'SEED_PEERS=%s\n' "$seed_peers"
printf 'PASSKEY_ENABLED=%s\n' "$passkey_enabled"
