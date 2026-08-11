#!/usr/bin/env bash
# standup.sh — BOOTSTRAP a LiveKit media plane on an island-DEDICATED box.
#
# Caddy issues turn.<domain> over HTTP-01 INDEPENDENTLY of LiveKit, so we wait for
# the cert FIRST, then boot LiveKit once with TURN enabled — no turn-disabled dance,
# and Docker never mkdir's an empty leaf dir that would poison Caddy's path (round-1
# Tesla). Ordering that IS load-bearing: exposure gate B passes BEFORE the firewall
# range opens (DESIGN §3.4). Fail-closed throughout; refuses shared boxes.
set -euo pipefail
cd "$(dirname "$0")"; . lib/cert-pair.sh
[ -f .env ] || { echo "no .env — copy .env.example and fill it." >&2; exit 1; }
set -a && . ./.env && set +a
: "${TURN_DOMAIN:?}" "${NODE_IP:?}" "${LIVEKIT_API_KEY:?}" "${LIVEKIT_API_SECRET:?}" "${CADDY_CERT_LEAF_DIR:?}"
: "${LIVEKIT_URL:?}" "${TURN_RELAY_START:?}" "${TURN_RELAY_END:?}"

case "$TURN_DOMAIN" in
  turn.imagineering.cc) echo "REFUSING: imagineering is shared infra — use docs/runbooks/imagineering-livekit-repair.md." >&2; exit 1 ;;
esac
[ "${I_AM_A_DEDICATED_ISLAND_BOX:-}" = "yes" ] || {
  echo "Set I_AM_A_DEDICATED_ISLAND_BOX=yes to confirm this box's LiveKit is island-dedicated (fail-closed)." >&2; exit 1; }

IMAGE="$(grep -oE 'livekit/livekit-server:[^[:space:]]+' docker-compose.yml | head -1)"

echo "== 1. wait for Caddy to issue $TURN_DOMAIN cert (HTTP-01, independent of LiveKit) =="
echo "   (add the 'turn.$TURN_DOMAIN' Caddy block + DNS A-record + port 80 first)"
for i in $(seq 1 60); do
  [ -s "$CADDY_CERT_LEAF_DIR/${TURN_DOMAIN}.crt" ] && [ -s "$CADDY_CERT_LEAF_DIR/${TURN_DOMAIN}.key" ] && { echo "cert files present."; break; }
  [ "$i" = 60 ] && { echo "cert never appeared at $CADDY_CERT_LEAF_DIR — Caddy block/DNS/port-80?" >&2; exit 1; }
  sleep 5
done
# Same bar as the restart path: a matched, unexpired pair valid FOR TURN_DOMAIN —
# not just non-empty files (round-2 Tesla: first boot must not serve a truncated /
# swapped / wrong-domain leaf for the process lifetime).
cert_pair_matches "$CADDY_CERT_LEAF_DIR/${TURN_DOMAIN}.crt" "$CADDY_CERT_LEAF_DIR/${TURN_DOMAIN}.key" "$TURN_DOMAIN" \
  || { echo "leaf pair invalid/mismatched/wrong-domain/expired — refusing to boot onto it (fail-closed)." >&2; exit 1; }
echo "cert pair valid for $TURN_DOMAIN."

echo "== 2. validate range + render config (turn enabled) with restrictive perms =="
# start<=end and sane bounds (round-3 Tesla: an inverted range still 'matches' .env
# but feeds iptables/LiveKit an undefined window).
[ "$TURN_RELAY_START" -le "$TURN_RELAY_END" ] && [ "$TURN_RELAY_START" -ge 1024 ] && [ "$TURN_RELAY_END" -le 65535 ] \
  || { echo "FATAL: TURN_RELAY_START/END ($TURN_RELAY_START-$TURN_RELAY_END) not a sane ordered 1024-65535 range." >&2; exit 1; }
( umask 077; envsubst '${TURN_DOMAIN} ${NODE_IP} ${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET} ${TURN_RELAY_START} ${TURN_RELAY_END}' \
    < livekit.yaml.tmpl > livekit.yaml )
chmod 600 livekit.yaml   # explicit: umask only guards CREATE; a re-run over an existing 0644 must not leave secrets world-readable

echo "== 3. assert ranges: rendered relay == .env, and DISJOINT from SFU ICE =="
ice_s=$(awk '/port_range_start:/{print $2; exit}' livekit.yaml); ice_e=$(awk '/port_range_end:/{print $2; exit}' livekit.yaml)
rly_s=$(awk '/relay_range_start:/{print $2}' livekit.yaml); rly_e=$(awk '/relay_range_end:/{print $2}' livekit.yaml)
[ "$rly_s" = "$TURN_RELAY_START" ] && [ "$rly_e" = "$TURN_RELAY_END" ] \
  || { echo "FATAL: rendered relay $rly_s-$rly_e != .env $TURN_RELAY_START-$TURN_RELAY_END (single-source broken)." >&2; exit 1; }
if [ "$ice_s" -le "$rly_e" ] && [ "$rly_s" -le "$ice_e" ]; then
  echo "FATAL: SFU ICE range $ice_s-$ice_e overlaps TURN relay range $rly_s-$rly_e." >&2; exit 1; fi
echo "   relay $rly_s-$rly_e matches .env and is disjoint from ICE $ice_s-$ice_e."

echo "== 4. preflight: the container user can READ the mounted key (Caddy keys are often 0600) =="
if ! docker run --rm -v "$CADDY_CERT_LEAF_DIR:/certs:ro" --entrypoint sh "$IMAGE" \
       -c "test -r /certs/${TURN_DOMAIN}.key && test -r /certs/${TURN_DOMAIN}.crt" 2>/dev/null; then
  echo "FATAL: container cannot read /certs/${TURN_DOMAIN}.{crt,key} (perms/UID). TURN TLS would silently never come up." >&2
  echo "   Fix cert perms/group so the LiveKit container UID can read them, then re-run." >&2; exit 1; fi
echo "   container can read the cert pair."

echo "== 5. boot livekit (turn enabled) =="
docker compose up -d
sleep 5

echo "== 6. exposure-acceptance (B) — range still CLOSED to real traffic =="
# Probe TURN_DOMAIN (not 127.0.0.1) so SNI matches the leaf; the box reaches its
# own host-network listener, and INPUT is still shut to the world (round-2 Tesla:
# a localhost probe risks TLS/name noise that never yields a clean auth-reject).
python3 e2e_media_relay.py --exposure-only --host "$TURN_DOMAIN" || {
  echo "exposure gate B FAILED/BLOCKED — NOT opening the firewall (fail-closed). Check B1 (unauth ALLOCATE reject), B2 (out-of-range ports), B3 (LiveKit >= v1.12 for CIDR-deny)." >&2; exit 1; }

echo "== 7. open firewall (double layer): UDP 3478, TCP 5349, UDP ${TURN_RELAY_START}-${TURN_RELAY_END} =="
echo "   OCI security-list opens need the cloud API/console (operator hands). Host iptables:"
cat <<FW
   for r in "udp 3478" "tcp 5349" "udp ${TURN_RELAY_START}:${TURN_RELAY_END}"; do set -- \$r
     sudo iptables -C INPUT -p \$1 --dport \$2 -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -p \$1 --dport \$2 -j ACCEPT
   done
FW
read -r -p "Confirm BOTH firewall layers open for the ranges above, then press enter for gate A… "

echo "== 8. connectivity-acceptance (A): forced relay-only media round-trip (livekit-rtc) =="
echo "   proves the UDP relay path; TLS/5349 relay is a KNOWN GAP (not advertised to clients)."
echo "   NOTE: needs a python with livekit + livekit-api + numpy (the box venv) on PATH as python3."
python3 e2e_media_relay.py --host "$TURN_DOMAIN"

echo "== BOOTSTRAP complete. Enable the renewal timer: =="
echo "   sudo cp cert-restart.service cert-restart.timer /etc/systemd/system/ && sudo systemctl enable --now cert-restart.timer"
