#!/usr/bin/env bash
# Resolve the gateway .env values that are OPERATOR CHOICES, so a standup re-run
# preserves them instead of silently reverting them to a default.
#
# WHY THIS FILE EXISTS. PR#151 found a class in standup.sh's media wiring and closed
# it there: "an operator CHOICE must be recorded and read back; a measured FACT should
# be re-derived." The class pass was then scoped to one code path. Testing that claim
# (#3734) found the seed-peers list with the same hole in the same file, and a
# systematic sweep of the .env heredoc found a second, PASSKEY_ENABLED. This is the
# sibling of resolve-media-env.sh for the non-media half.
#
# WHAT MAKES THESE TWO WORSE THAN THE HOSTNAME BUG. Both fail SILENTLY and look
# healthy:
#   * ISLAND_SEED_PEERS is the federation link. Each live island's seed list is the
#     other island. Reset to [], peers_service just serves self — no fetch, no error,
#     no log line. The islands simply stop knowing about each other.
#   * PASSKEY_ENABLED is the PRIMARY sign-in ingress since social was dropped
#     (#1923). Reset to false, /v1/auth/providers stops advertising passkeys and
#     nobody can sign in, on an island whose health check is green.
#
# NOT IN THE CLASS, checked rather than assumed:
#   * JWT_SECRET       — already read back and reused (standup.sh, "not rotated").
#   * GATEWAY_BASE_URL / ISLAND_DISPLAY_NAME — fail CLOSED: required, prompted, or
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

# ONE reader, sourced not copied — deploy/lib/dotenv-read.sh carries the grammar and the
# reason it exists. Four copies of this used to exist across deploy/, each treating a
# matcher miss as "never recorded"; the worst instance minted a fresh JWT secret.
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _d="$(cd -P "$(dirname "$_src")" && pwd)"; _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_d/$_src" ;; esac
done
. "$(cd -P "$(dirname "$_src")" && pwd)/lib/dotenv-read.sh"
_read_kv() { dotenv_read "$1" "$2"; }

# RESOLUTION ORDER: flag, then the value this island RECORDED, then convention.
# The middle rung is the one that was missing — an unread seed list resolves to [],
# peers_service serves self, and the islands stop knowing about each other with no
# error and a green health check.
seed_peers="$seed_flag"
[ -n "$seed_peers" ] || seed_peers="$(_read_kv "$gw_env" ISLAND_SEED_PEERS)"

# REFUSE, rather than fall through, when the ONLY recorded seed list is under the
# pre-2026-09 spelling. Deleting the legacy rung (#3836) is correct — but deleting a
# silent RECOVERY must not leave a silent LOSS in its place. Without this stop, an
# un-cut-over box's documented unflagged re-run resolves to the "[]" convention below,
# standup.sh writes that back as ISLAND_SEED_PEERS, and the federation link is emptied
# with no error and a green health check: #3734 exactly, re-opened by the cleanup that
# followed the rename. The bracket-shape guard further down cannot catch it — "[]" is
# bracket-delimited and perfectly valid.
#
# Not a fallback: the legacy VALUE is never read or written. The operator is told what
# to rename, and an explicit --seed-peers flag still wins outright (it is checked
# first, and an operator who passes it has stated their intent).
if [ -z "$seed_peers" ] && [ -n "$(_read_kv "$gw_env" GATEWAY_SEED_PEERS)" ]; then
  echo "resolve-gateway-env: this .env records its seed peers under the retired GATEWAY_SEED_PEERS name and has no ISLAND_SEED_PEERS. Continuing would resolve the federation list to [] and silently unpeer this island on the next standup write. Rename the key to ISLAND_SEED_PEERS in $gw_env (the value is unchanged), or pass --seed-peers with the intended array." >&2
  exit 1
fi
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
# read back as a truncated first line — `ISLAND_SEED_PEERS=[` silently replacing the
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

# REFUSE a seed list that is not BRACKET-DELIMITED. Scope stated exactly, because Carnot
# (round 5) correctly caught the earlier wording claiming "must be a JSON array" while the
# check only looks at the first and last character — so `[not-json]` passes.
#
# That gap is DELIBERATE and stays. This guard exists for ONE historic corruption: An island stood up before this fix with
# the guide's multiline invocation has a bare `ISLAND_SEED_PEERS=[` in its .env (the body
# lines were orphaned and unparseable by python-dotenv too). Reading that back and
# writing it out again would launder a broken value into a deliberate-looking one, and
# peers_service would keep serving self. Validating the JSON *body* means a JSON parser in
# shell, which is precisely what #3592 says not to build — so the check is bracket-shape
# only and the message, the comments and the test names all now say bracket-delimited
# rather than JSON. Prose matching code is the fix here, not more code.
case "$seed_peers" in
  "["*"]") ;;
  *) echo "resolve-gateway-env: ISLAND_SEED_PEERS is not bracket-delimited (must start '[' and end ']'), got '$seed_peers'. A pre-2026-09 standup with a multiline --seed-peers left a truncated value; pass --seed-peers with the intended array to repair it." >&2; exit 1 ;;
esac

printf 'SEED_PEERS=%s\n' "$seed_peers"
printf 'PASSKEY_ENABLED=%s\n' "$passkey_enabled"
