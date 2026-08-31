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

# Read the LAST assignment of a key, matching how python-dotenv resolves duplicates,
# and strip ONE matching pair of surrounding quotes, as python-dotenv also does.
#
# DEBT, NAMED RATHER THAN HIDDEN (#3592 — "Two parsers for one .env: the deploy
# preflight re-implements python-dotenv in shell"). This is now the THIRD shell
# approximation of that parser, and the divergence is DEMONSTRATED, not theoretical:
# before the strip below, a hand-edited PASSKEY_ENABLED="true" — which python-dotenv
# reads as true — was read here as the literal "true" and ABORTED standup. The guide
# explicitly invites that hand edit ("equivalently: set PASSKEY_ENABLED=true in .env").
#
# Quote-stripping closes the case that actually bites. TWO divergences from
# python-dotenv remain, both measured and both deliberate stopping points rather than
# oversights: inline comments (PASSKEY_ENABLED=true  # why) are read literally, and
# backslash escapes inside double quotes are not decoded, so a quoted JSON value
# ("[{\"id\":\"e\"}]") keeps its backslashes here while python-dotenv strips them.
# Neither shape is ever produced by the heredoc that writes this file. Every further
# case is one more parameter in a parser we should not be growing; the real fix is
# #3592's, one parser rather than four.
_read_kv() {
  local v q
  # WHY A GRAMMAR AND NOT A LIST OF CASES. Every shape python-dotenv accepts that this
  # reader does not is a chance to read NOTHING, fall through to convention, and write
  # PASSKEY_ENABLED=false + GATEWAY_SEED_PEERS=[] — #3734 itself, silently, through
  # supported .env syntax. Found one at a time, in this order:
  #   `export KEY=v`        — cage-match round 3 (Carnot)
  #   `  KEY=v`             — the parity corpus, in seconds, after three rounds missed it
  #   `KEY = v` / `KEY =v`  — cage-match round 4 (Carnot), and SILENT
  # The round-4 one is the instructive failure: the corpus varied PREFIX and QUOTING and
  # whitespace-before-KEY, but never DELIMITER SPACING. Twelve rows along one axis is not
  # a bounded class. Carnot: "the number of corpus rows is less important than the
  # grammar boundary." So the grammar is written once, above, and the corpus now varies
  # the axis rather than sampling the shape.
  # ONE grammar, matched and stripped by the same expression so they cannot drift:
  #   [ws] [export ws] KEY [ws] = [ws] VALUE [ws]
  # Every element is optional-whitespace-tolerant because python-dotenv is. Trailing
  # whitespace and a stray CR are stripped too, so a .env touched on Windows or moved
  # badly does not abort a deploy.
  local _re="^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=[[:space:]]*"
  v=$([ -f "$1" ] && grep -E "$_re" "$1" 2>/dev/null | tail -n1 | sed -E "s/$_re//; s/[[:space:]]*\r?$//" || true)
  for q in '"' "'"; do
    if [ ${#v} -ge 2 ] && [ "${v#"$q"}" != "$v" ] && [ "${v%"$q"}" != "$v" ]; then
      v="${v#"$q"}"; v="${v%"$q"}"; break
    fi
  done
  printf '%s' "$v"
}

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

# REFUSE a seed list that is not a JSON array. An island stood up before this fix with
# the guide's multiline invocation has `GATEWAY_SEED_PEERS=[` in its .env (the body
# lines were orphaned and unparseable by python-dotenv too). Reading that back and
# writing it out again would launder a broken value into a deliberate-looking one, and
# peers_service would keep serving self. Shape-only: this is not a JSON parser, and
# growing one here is exactly what #3592 says not to do.
case "$seed_peers" in
  "["*"]") ;;
  *) echo "resolve-gateway-env: GATEWAY_SEED_PEERS must be a JSON array (starts '[', ends ']'), got '$seed_peers'. A pre-2026-09 standup with a multiline --seed-peers left a truncated value; pass --seed-peers with the intended array to repair it." >&2; exit 1 ;;
esac

printf 'SEED_PEERS=%s\n' "$seed_peers"
printf 'PASSKEY_ENABLED=%s\n' "$passkey_enabled"
