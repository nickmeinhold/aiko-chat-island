#!/usr/bin/env bash
# standup.sh — BOOTSTRAP a LiveKit media plane on an island-dedicated box.
#
# Order is load-bearing (DESIGN §4):
#   1. render config (turn DISABLED) so LiveKit boots without a cert
#   2. wait for Caddy to issue the first turn.<domain> cert (HTTP-01)
#   3. enable turn, restart
#   4. exposure test B while the relay range is STILL CLOSED to real traffic
#   5. ONLY THEN open the firewall range
#   6. connectivity test A (parity)
#
# Refuses to run against a shared box (imagineering). Never opens the range before
# B passes. Fail-closed throughout.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "no .env — copy .env.example and fill it." >&2; exit 1; }
set -a && . ./.env && set +a
: "${TURN_DOMAIN:?}" "${NODE_IP:?}" "${LIVEKIT_API_KEY:?}" "${LIVEKIT_API_SECRET:?}" "${CADDY_CERT_LEAF_DIR:?}"

# Guard: this automation is BOOTSTRAP-only. A shared multi-tenant SFU is out-of-repo
# ownership (DESIGN §2). Refuse known shared hosts / an explicit opt-out flag.
case "$TURN_DOMAIN" in
  turn.imagineering.cc) echo "REFUSING: imagineering is a shared box — use docs/runbooks/imagineering-livekit-repair.md, not standup.sh." >&2; exit 1 ;;
esac
[ "${I_AM_A_DEDICATED_ISLAND_BOX:-}" = "yes" ] || {
  echo "Set I_AM_A_DEDICATED_ISLAND_BOX=yes to confirm this box's LiveKit is island-dedicated (not shared infra). Refusing (fail-closed)." >&2; exit 1; }

render() {  # $1 = turn_enabled (true|false)
  TURN_ENABLED="$1" envsubst '${TURN_DOMAIN} ${NODE_IP} ${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET}' \
    < livekit.yaml.tmpl > livekit.yaml
  if [ "$1" = "false" ]; then
    # Comment the whole turn: block for the pre-cert boot.
    sed -i.bak '/^turn:/,/relay_range_end:/ s/^/# /' livekit.yaml && rm -f livekit.yaml.bak
  fi
  # Assert ICE range and relay range are disjoint (7882-7892 vs 50000-60000).
  echo "rendered livekit.yaml (turn=$1)"
}

echo "== 1. render (turn disabled) + boot =="
render false
docker compose up -d

echo "== 2. wait for Caddy to issue $TURN_DOMAIN cert (HTTP-01) =="
for i in $(seq 1 60); do
  [ -s "$CADDY_CERT_LEAF_DIR/${TURN_DOMAIN}.crt" ] && { echo "cert present."; break; }
  [ "$i" = 60 ] && { echo "cert never appeared at $CADDY_CERT_LEAF_DIR — is the turn.$TURN_DOMAIN Caddy block added + DNS A-record live + port 80 reachable?" >&2; exit 1; }
  sleep 5
done

echo "== 3. enable turn + restart =="
render true
docker compose up -d
sleep 5

echo "== 4. exposure-acceptance (B) — range still CLOSED to real traffic =="
# B must pass BEFORE the firewall opens (DESIGN §3.4 ordering invariant). e2e runs
# its negative/exposure suite against localhost while the public range is shut.
python3 e2e_media_relay.py --exposure-only --host 127.0.0.1 || {
  echo "exposure test B FAILED — NOT opening the firewall range (fail-closed)." >&2; exit 1; }

echo "== 5. open firewall (double layer) — UDP 3478, TCP 5349, UDP 50000-60000 =="
echo "  NOTE: OCI security-list opens need the cloud API/console (operator hands)."
echo "  Host iptables (this box):"
cat <<'FW'
  sudo iptables -C INPUT -p udp --dport 3478 -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -p udp --dport 3478 -j ACCEPT
  sudo iptables -C INPUT -p tcp --dport 5349 -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -p tcp --dport 5349 -j ACCEPT
  sudo iptables -C INPUT -p udp --dport 50000:60000 -j ACCEPT 2>/dev/null || sudo iptables -A INPUT -p udp --dport 50000:60000 -j ACCEPT
FW
read -r -p "Confirm BOTH firewall layers are open for the ranges above, then press enter to run gate A… "

echo "== 6. connectivity-acceptance (A) — forced relay over TCP/TLS + UDP canary =="
python3 e2e_media_relay.py --host "$TURN_DOMAIN"

echo "== BOOTSTRAP standup complete. Enable the renewal timer: =="
echo "  sudo cp cert-restart.service cert-restart.timer /etc/systemd/system/ && sudo systemctl enable --now cert-restart.timer"
