#!/usr/bin/env bash
# haproxy-cert-sync.sh — the SOLE writer of /etc/haproxy/certs/turn.enspyr.co.pem.
#
# The P1 fix for the cage-match's "cert-renewal time bomb": once HAProxy owns :443, Caddy
# still RENEWS the turn.enspyr.co cert (loopback site block, HTTP-01 forced) but HAProxy
# serves a SEPARATE concatenated PEM. Without a sync, HAProxy keeps serving the OLD cert
# after Caddy renews — TURNS silently dies ~89d out (imagineering-Jul-24 class), every
# dashboard green. This script closes that: fingerprint-gated, it rebuilds the PEM from
# Caddy's store the moment the leaf cert changes and gracefully reloads HAProxy. Run it
# on a timer (haproxy-cert-sync.timer). Idempotent: a no-change run is a cheap fingerprint
# compare and exits 0 without touching HAProxy.
#
# SECURITY (Tesla F8): this copies caddy's PRIVATE KEY across a uid boundary into haproxy's
# space. The PEM is written 0640 root:haproxy via a private temp + atomic rename, so the key
# is never world-readable and never half-written. This script is the ONLY thing that writes
# that path.
set -euo pipefail

DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
CADDY_CERT_DIR="${CADDY_CERT_DIR:-/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/${DOMAIN}}"
CRT="${CADDY_CERT_DIR}/${DOMAIN}.crt"
KEY="${CADDY_CERT_DIR}/${DOMAIN}.key"
PEM_DIR="${HAPROXY_CERT_DIR:-/etc/haproxy/certs}"
PEM="${PEM_DIR}/${DOMAIN}.pem"

log() { echo "[haproxy-cert-sync] $*"; }

[ "$(id -u)" -eq 0 ] || { log "must run as root (reads caddy's key, writes haproxy pem, reloads haproxy)"; exit 1; }

for f in "$CRT" "$KEY"; do
  [ -s "$f" ] || { log "FATAL: source cert missing/empty: $f — is Caddy still managing $DOMAIN? (aborting; keeping existing PEM)"; exit 1; }
done

# Leaf-cert fingerprint of what Caddy holds now vs what HAProxy is serving. Compare the LEAF
# only (openssl x509 reads the first cert in each file) — sufficient and stable across chain
# reordering.
src_fp="$(openssl x509 -in "$CRT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
[ -n "$src_fp" ] || { log "FATAL: could not fingerprint source cert $CRT"; exit 1; }
cur_fp=""
[ -s "$PEM" ] && cur_fp="$(openssl x509 -in "$PEM" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 || true)"

if [ "$src_fp" = "$cur_fp" ]; then
  log "up-to-date ($DOMAIN leaf $src_fp) — no reload."
  exit 0
fi

log "cert changed (was ${cur_fp:-none}, now $src_fp) — rebuilding PEM + reloading HAProxy."
install -d -m 0750 -o root -g haproxy "$PEM_DIR"
tmp="$(mktemp "${PEM_DIR}/.${DOMAIN}.pem.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# fullchain THEN key (HAProxy accepts either order, but keep it conventional).
cat "$CRT" "$KEY" > "$tmp"
chown root:haproxy "$tmp"
chmod 0640 "$tmp"
# Validate the new PEM parses as a usable keypair BEFORE swapping it in (fail-closed: a
# broken PEM must not replace a working one).
openssl x509 -in "$tmp" -noout >/dev/null 2>&1 || { log "FATAL: rebuilt PEM has no valid cert — keeping old"; exit 1; }
openssl pkey -in "$tmp" -noout >/dev/null 2>&1 || { log "FATAL: rebuilt PEM has no valid key — keeping old"; exit 1; }
mv "$tmp" "$PEM"
trap - EXIT

# Graceful reload — HAProxy finishes in-flight conns on the old workers, new conns get the
# new cert. No dropped connections. If HAProxy isn't running yet (pre-cutover), skip the
# reload (the PEM is staged for cutover to pick up).
if systemctl is-active --quiet haproxy; then
  systemctl reload haproxy
  log "reloaded haproxy with new $DOMAIN cert."
else
  log "haproxy not active — PEM staged, skipping reload (cutover will start it)."
fi
