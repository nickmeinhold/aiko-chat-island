#!/usr/bin/env bash
# provision.sh — build a faithful, DISPOSABLE replica of the enspyr box inside a VM, so
# cutover.sh / rollback.sh can be run FOR REAL (real systemd, real iptables, real reboot)
# without touching a live island. Task #6: the off-:443 empirical proof.
#
# Fidelity notes (the honest scope of what a rehearsal here can prove):
#   SAME: Ubuntu 24.04, systemd, Caddy (official pkg), HAProxy (apt), Docker + host-networked
#         livekit/livekit-server:v1.13.5, iptables+ip6tables+netfilter-persistent, real :443.
#   REAL ACME: a local Pebble ACME server + dnsmasq, so cert issuance/renewal exercises the
#         genuine HTTP-01-on-:80 path. `tls internal` would SKIP the exact path INV-4 tests.
#   DIFFERENT: arm64 not x86_64; no public IP (an "external" vantage point is a docker
#         container reaching the host over docker0 — non-loopback, so `! -i lo` DROP applies);
#         the gateway behind Caddy is an IP-echo stand-in, not the real island image.
#   DELTA to the artifact under test: both Caddyfiles get `acme_ca`/`acme_ca_root` injected to
#         point at Pebble (see caddy_acme_patch). Everything else is the artifact verbatim.
set -euo pipefail

VM_IP="${VM_IP:?set VM_IP to the primary IP of the guest}"
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
CHAT_DOMAIN="${CHAT_DOMAIN:-chat.enspyr.co}"
LK_DOMAIN="${LK_DOMAIN:-livekit.enspyr.co}"
LIVEKIT_DIR=/home/ubuntu/apps/livekit
log() { echo "[provision] $*"; }

# ------------------------------------------------------------------ packages
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# iptables-persistent must not prompt (it asks to save current rules)
echo "iptables-persistent iptables-persistent/autosave_v4 boolean false" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean false" | debconf-set-selections
apt-get install -y -qq haproxy docker.io docker-compose-v2 iptables-persistent dnsmasq \
  openssl curl jq debian-keyring debian-archive-keyring apt-transport-https ca-certificates >/dev/null

if ! command -v caddy >/dev/null 2>&1; then
  log "installing caddy from the official repo"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

# ------------------------------------------------------------------ name resolution
# /etc/hosts so on-box curl/openssl resolve the three names to this VM.
log "wiring /etc/hosts + dnsmasq (for Pebble's HTTP-01 validation lookups)"
sed -i "/# turn-rehearsal/d" /etc/hosts
echo "${VM_IP} ${CHAT_DOMAIN} ${LK_DOMAIN} ${TURN_DOMAIN} # turn-rehearsal" >> /etc/hosts

# Pebble resolves the ACME identifier via DNS before hitting :80. Point it at a dnsmasq that
# maps the whole zone to this VM. Port 5353 to stay clear of systemd-resolved on :53.
cat > /etc/dnsmasq.d/turn-rehearsal.conf <<EOF
port=5353
listen-address=127.0.0.1
bind-interfaces
address=/enspyr.co/${VM_IP}
EOF
systemctl restart dnsmasq

# ------------------------------------------------------------------ Pebble (local ACME CA)
# TWO DISTINCT TRUST CHAINS here — conflating them is the easy mistake:
#   (a) Pebble's ENDPOINT cert: the TLS cert on :14000/:15000. Caddy must trust it to TALK to
#       the ACME server at all → this is what Caddy's `acme_ca_root` points at. Self-signed here.
#   (b) Pebble's ISSUANCE root: the CA that signs the certs Pebble mints, fetched from
#       /roots/0 and minted fresh per container start → goes in the SYSTEM trust store so
#       curl/openssl accept chat.enspyr.co. It is NOT the same file as (a).
log "starting Pebble ACME server"
mkdir -p /opt/pebble
# (a) endpoint cert — the image is distroless and ships no test certs, so mint our own.
if [ ! -s /opt/pebble/endpoint-cert.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 90 \
    -keyout /opt/pebble/endpoint-key.pem -out /opt/pebble/endpoint-cert.pem \
    -subj "/CN=localhost" -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1
fi
cat > /opt/pebble/pebble-config.json <<'EOF'
{
  "pebble": {
    "listenAddress": "0.0.0.0:14000",
    "managementListenAddress": "0.0.0.0:15000",
    "certificate": "/pebble/endpoint-cert.pem",
    "privateKey": "/pebble/endpoint-key.pem",
    "httpPort": 80,
    "tlsPort": 443,
    "ocspResponderURL": "",
    "externalAccountBindingRequired": false
  }
}
EOF
docker rm -f pebble >/dev/null 2>&1 || true
# host network so Pebble's VA can reach :80 on this VM for HTTP-01, and so it can use dnsmasq.
# PEBBLE_VA_NOSLEEP kills the random validation delay; validation itself stays REAL.
docker run -d --name pebble --restart unless-stopped --network host \
  -e PEBBLE_VA_NOSLEEP=1 \
  -v /opt/pebble:/pebble:ro \
  ghcr.io/letsencrypt/pebble:latest \
  -config /pebble/pebble-config.json -dnsserver 127.0.0.1:5353 >/dev/null
# (b) issuance root — minted per container start, so always re-fetch (never reuse a stale one).
rm -f /opt/pebble/issuance-root.pem
for _ in $(seq 1 30); do
  if curl -sk --max-time 3 https://127.0.0.1:15000/roots/0 -o /opt/pebble/issuance-root.pem \
     && grep -q "BEGIN CERTIFICATE" /opt/pebble/issuance-root.pem 2>/dev/null; then break; fi
  sleep 1
done
grep -q "BEGIN CERTIFICATE" /opt/pebble/issuance-root.pem 2>/dev/null || {
  echo "[provision] FATAL: could not fetch Pebble issuance root"; docker logs pebble 2>&1 | tail -20; exit 1; }
cp /opt/pebble/issuance-root.pem /usr/local/share/ca-certificates/pebble-issuance-root.crt
update-ca-certificates >/dev/null 2>&1
log "Pebble up; issuance root installed into the system trust store"

# ------------------------------------------------------------------ the gateway stand-in
# Echoes the peer address + forwarded headers, so INV-5 (real client IP through PROXY
# protocol) is provable: we read what Caddy actually reports to its upstream.
log "installing the :8095 IP-echo gateway stand-in"
cat > /opt/echo-gateway.py <<'EOF'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "peer": self.client_address[0],
            "x_forwarded_for": self.headers.get("X-Forwarded-For"),
        }).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 8095), H).serve_forever()
EOF
cat > /etc/systemd/system/echo-gateway.service <<'EOF'
[Unit]
Description=IP-echo stand-in for the island gateway (rehearsal)
[Service]
ExecStart=/usr/bin/python3 /opt/echo-gateway.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now echo-gateway >/dev/null 2>&1

# ------------------------------------------------------------------ LiveKit (mirrors enspyr)
log "installing LiveKit (host-networked, mirrors enspyr's config)"
mkdir -p "$LIVEKIT_DIR"
cat > "${LIVEKIT_DIR}/livekit.yaml" <<EOF
# LiveKit SFU — REHEARSAL replica of the enspyr island config
port: 7880
rtc:
  port_range_start: 7882
  port_range_end: 7892
  use_external_ip: false
  node_ip: ${VM_IP}
  tcp_port: 7881
turn:
  enabled: true
  domain: ${TURN_DOMAIN}
  udp_port: 3478
  tls_port: 5349
  cert_file: /certs/${TURN_DOMAIN}.crt
  key_file: /certs/${TURN_DOMAIN}.key
  relay_range_start: 50000
  relay_range_end: 60000
  deny_peer_cidrs:
    - 100.64.0.0/10
keys:
  APIrehearsalkey01: rehearsalsecret_0000000000000000000000000000000000000000000
logging:
  level: info
  json: true
EOF
cat > "${LIVEKIT_DIR}/docker-compose.yml" <<EOF
services:
  livekit:
    image: livekit/livekit-server:v1.13.5
    container_name: livekit
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./livekit.yaml:/etc/livekit.yaml:ro
      - /opt/turncerts:/certs:ro
    command: --config /etc/livekit.yaml
EOF

# ------------------------------------------------------------------ Caddy (the STOCK state)
log "installing the stock Caddyfile (pre-cutover state)"
cat > /etc/caddy/Caddyfile <<EOF
{
	acme_ca https://127.0.0.1:14000/dir
	acme_ca_root /opt/pebble/endpoint-cert.pem
	email rehearsal@enspyr.co
}

${CHAT_DOMAIN} {
	reverse_proxy localhost:8095
}

${LK_DOMAIN} {
	reverse_proxy 127.0.0.1:7880
}

${TURN_DOMAIN} {
	respond "turn" 200
}
EOF
systemctl restart caddy
log "waiting for Caddy to issue all three certs from Pebble"
ok=0
for _ in $(seq 1 60); do
  if timeout 4 openssl s_client -connect 127.0.0.1:443 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "[provision] FATAL: Caddy never served a cert for $TURN_DOMAIN"; journalctl -u caddy -n 40 --no-pager; exit 1; }

# Caddy's on-disk store dir is named after the ACME CA endpoint, so under Pebble it is NOT
# the production `acme-v02.api.letsencrypt.org-directory`. Discover it and pin a stable
# symlink; both LiveKit's bind-mount and cert-sync's CADDY_CERT_DIR point at the symlink, so
# no artifact needs editing for the rehearsal.
# Pristine copies of the two mutable config artifacts. reset.sh restores from HERE, never via
# rollback.sh — using the artifact under test to reset between its own tests would be circular
# (a broken rollback would "pass" by leaving the same broken state each run).
install -d /opt/rehearsal-pristine
cp /etc/caddy/Caddyfile          /opt/rehearsal-pristine/Caddyfile
cp "${LIVEKIT_DIR}/livekit.yaml" /opt/rehearsal-pristine/livekit.yaml

CERTDIR="$(find /var/lib/caddy/.local/share/caddy/certificates -type d -name "$TURN_DOMAIN" 2>/dev/null | head -1)"
[ -n "$CERTDIR" ] || { echo "[provision] FATAL: no caddy cert dir for $TURN_DOMAIN"; exit 1; }
ln -sfn "$CERTDIR" /opt/turncerts
log "caddy cert store: $CERTDIR  ->  /opt/turncerts"

# LiveKit needs the turn cert on disk; start it now that certs exist.
cd "$LIVEKIT_DIR" && docker compose up -d >/dev/null 2>&1 || true
sleep 5
docker ps --format '{{.Names}} {{.Status}}' | grep -q '^livekit' \
  || { echo "[provision] WARN: livekit not running"; docker logs livekit 2>&1 | tail -20; }

log "PROVISION COMPLETE"
log "  caddy cert dir : /opt/turncerts (-> $CERTDIR)"
log "  run cutover with: CADDY_CERT_DIR=/opt/turncerts"
