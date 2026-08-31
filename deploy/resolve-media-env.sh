#!/usr/bin/env bash
# Resolve an island's media hostnames and decide WHICH file holds the live LiveKit
# credential. Pure decision logic, extracted from standup.sh so it can be tested.
#
# WHY THIS FILE EXISTS. Every finding across three cage-match rounds on PR#151 was in
# standup.sh's media wiring, and standup.sh cannot be tested: it needs a live Docker
# daemon and has no dry-run. So four fixes shipped with nothing guarding them, and
# round 1's bug came back inside round 3's fix precisely because nothing could catch
# it. This is the same shape as deploy/preflight-apns.sh — a small script taking file
# paths, exiting non-zero on refusal — for the same reason.
#
# IT DOES NOT EMIT THE SECRET. It reports the SOURCE ("sfu" / "gateway" / "none") and
# the caller reads the pair from that file. The bugs were all in the decision, never
# in the read, so the decision is what needs a test — and this way a credential never
# enters a pipe, a log, or a test fixture.
#
# Usage:
#   resolve-media-env.sh <gateway-env> <livekit-env> <domain> [turn-flag] [livekit-flag]
#
# Prints on stdout (KEY=VALUE, one per line):
#   TURN_DOMAIN=<host>
#   LIVEKIT_DOMAIN=<host>
#   CRED_SOURCE=sfu|gateway|none
#
# Exits 1 with a message on stderr when the credential state is unusable.
set -euo pipefail

gw_env="${1:?usage: resolve-media-env.sh <gateway-env> <livekit-env> <domain> [turn-flag] [livekit-flag]}"
lk_env="${2:?missing <livekit-env>}"
domain="${3:?missing <domain>}"
turn_flag="${4:-}"
livekit_flag="${5:-}"

. "$(dirname "${BASH_SOURCE[0]}")/lib/dotenv-read.sh"   # ONE .env grammar (see that file)
_read_kv() { dotenv_read "$1" "$2"; }

# RESOLUTION ORDER, one order for both hostnames: flag, then the value this island
# RECORDED, then convention.
#
# The middle rung is the one that kept going missing. Round 3 of the PR#151 cage-match
# added persistence of LIVEKIT_DOMAIN and taught only the RECOVERY branch to read it
# back; the --with-media branch still defaulted by convention and then overwrote the
# recorded value with it — destroying the record the persistence existed to create, on
# the documented safe-to-re-run path. That was round 1's hostname-derivation bug
# reappearing inside round 3's fix.
#
# The line that closes the class: an operator CHOICE must be recorded and read back; a
# measured FACT should be re-derived. Hostnames are choices, so they live here.
# LIVEKIT_NODE_IP is a measured fact about the host and deliberately does NOT — it is
# re-fetched every run, which is correct when a box's public IP changes.
turn_domain="$turn_flag"
[ -n "$turn_domain" ] || turn_domain="$(_read_kv "$lk_env" LIVEKIT_TURN_DOMAIN)"
[ -n "$turn_domain" ] || turn_domain="turn.$domain"

# A SEPARATE name, never a synonym. Both live islands serve the SFU websocket on
# livekit.<host> and TURN on turn.<host>; conflating them hands every client the wrong
# endpoint (PR#151 round 1, Carnot — verified against both islands' .env).
livekit_domain="$livekit_flag"
[ -n "$livekit_domain" ] || livekit_domain="$(_read_kv "$lk_env" LIVEKIT_DOMAIN)"
[ -n "$livekit_domain" ] || livekit_domain="livekit.$domain"

# A CREDENTIAL IS A PAIR, NOT TWO FIELDS. Resolving key and secret independently lets a
# half-written file on one side combine with the other into a HYBRID that matches
# neither, which then gets written to both files and authenticates nothing (PR#151
# round 2, Carnot). Each file is read as a whole: complete, empty, or partial.
gw_key="$(_read_kv "$gw_env" LIVEKIT_API_KEY)"
gw_secret="$(_read_kv "$gw_env" LIVEKIT_API_SECRET)"
sfu_key="$(_read_kv "$lk_env" LIVEKIT_API_KEY_ID)"
sfu_secret="$(_read_kv "$lk_env" LIVEKIT_API_SECRET)"

_pair_state() {
  if   [ -n "$1" ] && [ -n "$2" ]; then echo complete
  elif [ -z "$1" ] && [ -z "$2" ]; then echo empty
  else echo partial; fi
}
gw_state="$(_pair_state "$gw_key" "$gw_secret")"
sfu_state="$(_pair_state "$sfu_key" "$sfu_secret")"

# A partial pair is CORRUPTION, not a starting point — half a credential cannot be
# completed from the other file without inventing one.
if [ "$gw_state" = partial ] || [ "$sfu_state" = partial ]; then
  echo "a LiveKit credential is half-present: gateway env is '$gw_state', $lk_env is '$sfu_state'." >&2
  echo "A key without its secret (or the reverse) cannot be completed from the other file" >&2
  echo "without inventing a pair that authenticates nothing. Restore or clear the damaged" >&2
  echo "file, then re-run." >&2
  exit 1
fi

# Both complete and disagreeing: refuse. Either could be the live one, and the wrong
# choice mints tokens the running SFU rejects, with no error anywhere.
if [ "$gw_state" = complete ] && [ "$sfu_state" = complete ] \
   && { [ "$gw_key" != "$sfu_key" ] || [ "$gw_secret" != "$sfu_secret" ]; }; then
  echo "the gateway env and $lk_env hold DIFFERENT LiveKit key pairs." >&2
  echo "Refusing to guess which is live. Reconcile them by hand (make both match the pair" >&2
  echo "the RUNNING SFU was started with), then re-run." >&2
  exit 1
fi

if   [ "$sfu_state" = complete ]; then cred_source=sfu
elif [ "$gw_state"  = complete ]; then cred_source=gateway
else cred_source=none; fi

echo "TURN_DOMAIN=$turn_domain"
echo "LIVEKIT_DOMAIN=$livekit_domain"
echo "CRED_SOURCE=$cred_source"
