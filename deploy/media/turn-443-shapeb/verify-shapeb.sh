#!/usr/bin/env bash
# verify-shapeb.sh — acceptance gate for the Shape-B TURN-on-443 layout (see RUNBOOK.md).
#
# FAIL-CLOSED, and deliberately so in one specific place: the only check that proves MEDIA is
# the real-client relay proof, and that must run from a UDP-blocked vantage OFF this box. A gate
# that quietly skips it when it cannot run is the exact defect F1 found on the mux shape — it
# passed on a dead turns:443. So this script REFUSES to report OK unless the client proof was
# supplied and passed. Green here without RELAY_PROOF=OK is not a thing.
#
# Nothing here is box-specific: every address and domain is a parameter. Do not hardcode.
#
# Env (all required unless noted):
#   TURN_DOMAIN   turn.<domain> — the name clients relay through
#   IP1           address that must keep serving the web front door (Caddy)
#   IP2           address LiveKit owns for TURN (:443 + :3478)
#   PUB2          public IP mapped to IP2 — what node_ip must advertise
#   LK_URL        wss://livekit.<domain>
#   LK_API_KEY / LK_API_SECRET   for reading the advertised ice_servers
#   RELAY_PROOF   "OK" once webrtc_relay_proof.py has passed from a UDP-blocked vantage.
#                 See the tail of this script for the exact command.
#   B3_PROBE      path to b3_relay_probe.py (default: ../b3_relay_probe.py)
set -uo pipefail

for v in TURN_DOMAIN IP1 IP2 PUB2 LK_URL LK_API_KEY LK_API_SECRET; do
  [ -n "${!v:-}" ] || { echo "FATAL: \$$v is unset — refusing to verify a box I cannot describe"; exit 2; }
done
HERE="$(cd "$(dirname "$0")" && pwd)"
B3_PROBE="${B3_PROBE:-$HERE/../b3_relay_probe.py}"
fails=0

# Run this script AS THE NORMAL USER and elevate only for `ss`. Running the whole thing under
# sudo is the obvious move (ss -tlnp needs root to show process names) and it silently breaks
# check 3: the livekit/aioice deps are installed in the invoking user's site-packages, root
# cannot import them, and the ice_servers read fails for a reason that has nothing to do with
# the box. A gate that loses a capability by elevating is a gate that reports on itself.
ss_p() { if [ "$(id -u)" -eq 0 ]; then ss "$@"; else sudo -n ss "$@" 2>/dev/null || ss "$@"; fi; }
red()  { echo "  FAIL  $*"; fails=$((fails+1)); }
green(){ echo "  ok    $*"; }

echo "== 1. the two :443 listeners coexist =="
listeners="$(ss_p -tlnp 2>/dev/null | grep ':443 ')"
echo "$listeners" | grep -q "$IP1:443.*caddy"          && green "Caddy on $IP1:443"      || red "Caddy is NOT on $IP1:443"
echo "$listeners" | grep -q "$IP2:443.*livekit-server" && green "LiveKit on $IP2:443"    || red "LiveKit is NOT on $IP2:443"
# A wildcard :443 means one of them grabbed every address — the collision this shape avoids.
echo "$listeners" | grep -qE '(\*|0\.0\.0\.0):443'     && red "something holds WILDCARD :443 — Shape B is not in force" \
                                                       || green "no wildcard :443 holder"

echo "== 2. the ACME spine is reachable on IP2:80 =="
# Without this, HTTP-01 for $TURN_DOMAIN fails the moment DNS points at PUB2 and the cert rots
# ~89d out, silently, while everything looks green today.
ss_p -tlnp 2>/dev/null | grep -q "$IP2:80 " && green "Caddy has a :80 listener on $IP2" \
                                          || red "NO :80 listener on $IP2 — HTTP-01 will fail (add the http:// block)"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 8 --resolve "$TURN_DOMAIN:80:$IP2" \
        "http://$TURN_DOMAIN/.well-known/acme-challenge/gate-probe" 2>/dev/null)"
# 404 is the CORRECT answer: the handler is live, this token just does not exist. A refused
# connection or a timeout (000) is the failure we care about.
case "$code" in
  000|"") red "ACME path on $IP2:80 is unreachable (curl code '$code')" ;;
  *)      green "ACME path answers on $IP2:80 (HTTP $code)" ;;
esac

echo "== 3. advertised ICE servers match the ACTUAL listeners =="
# The node_ip trap: bind_addresses moves the UDP TURN listener, but the advertised UDP URL is
# built from rtc.node_ip. A mismatch kills the UDP relay for every user, silently.
ice="$(LK_URL="$LK_URL" LK_API_KEY="$LK_API_KEY" LK_API_SECRET="$LK_API_SECRET" \
       B3_PROBE="$B3_PROBE" python3 - <<'PY' 2>/dev/null
import asyncio, importlib.util, os, sys
spec = importlib.util.spec_from_file_location("b3", os.environ["B3_PROBE"])
b3 = importlib.util.module_from_spec(spec); spec.loader.exec_module(b3)
async def main():
    servers = await b3.extract_turn_creds()
    if not servers: return 1
    for s in servers: print(" ".join(s.urls))
    return 0
sys.exit(asyncio.run(main()))
PY
)"
if [ -z "$ice" ]; then
  red "could not read ice_servers from the SFU JoinResponse"
else
  echo "        advertised: $ice"
  echo "$ice" | grep -q "turns:$TURN_DOMAIN:443" \
    && green "TURNS advertised as turns:$TURN_DOMAIN:443" \
    || red "TURNS is NOT advertised on :443 — clients will never reach the TLS relay"
  # LiveKit builds the UDP url from node_ip; it must name PUB2, which NATs to IP2 where the
  # listener actually is.
  if echo "$ice" | grep -q "turn:$PUB2:3478"; then
    green "UDP relay advertised at PUB2 ($PUB2) — matches the listener"
  else
    red "UDP relay advertised at an address that is NOT PUB2 — rtc.node_ip is not aligned with bind_addresses; the UDP relay path is DEAD (all relay traffic will silently degrade to TCP)"
  fi
fi
ss_p -ulnp 2>/dev/null | grep -q "$IP2:3478" && green "TURN UDP listening on $IP2:3478" \
                                           || red "no TURN UDP listener on $IP2:3478"

echo "== 4. a REAL client relayed over turns:443 (the only check that proves media) =="
if [ "${RELAY_PROOF:-}" = "OK" ]; then
  green "client relay proof supplied as PASSED"
else
  red "RELAY_PROOF is not OK — steps 1-3 prove PLUMBING, not a call. Run, from a vantage with UDP to the box blocked:

    LK_URL='$LK_URL' LK_TOKEN='<minted>' TURN_DOMAIN='$TURN_DOMAIN' \\
      python3 $HERE/../webrtc_relay_proof.py

  It must report result OK with selected_local.relayProtocol == 'tls'. Run the UDP-OPEN control
  too: it must select 'udp'. A run that cannot fail is not evidence. Block UDP scoped to the
  browser (iptables -m owner --uid-owner) — a whole-host DROP severs LiveKit's own TURN->SFU hop
  and reads as a failure that is not one."
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "SHAPE_B=OK — plumbing verified AND a real call proved to relay over turns:$TURN_DOMAIN:443"
  exit 0
fi
echo "SHAPE_B=FAILED ($fails check(s)) — do not claim TURN works on this box"
exit 1
