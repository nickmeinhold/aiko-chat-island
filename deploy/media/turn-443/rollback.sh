#!/usr/bin/env bash
# rollback.sh — restore ALL FOUR coupled artifacts of the Shape-C cutover, in an order that
# NEVER leaves plaintext TURN on a public socket and never leaves :443 owned by two things.
#
# The cage-match's P0: the old runbook restored only THREE artifacts (Caddy, HAProxy, firewall)
# and left livekit.yaml on external_tls:true → a "successful" rollback left LiveKit speaking
# PLAINTEXT TURN on public :5349. The four artifacts are:
#   1. HAProxy         — stop it, freeing public :443
#   2. Caddy           — restore Caddyfile.stock, back onto public :443
#   3. livekit.yaml    — restore .stock (TLS-on-5349 + own cert), restart LiveKit
#   4. firewall :5349  — reopen to public — LAST, and only AFTER (3) has put TLS back on 5349,
#                        so the plaintext socket is never publicly reachable for even an instant.
#
# Idempotent + safe to run STANDALONE (a paranoid operator can just run it): every step checks
# current state before acting. Fail-closed: if a step can't be verified it ABORTS loudly rather
# than proceeding to reopen the firewall over a still-plaintext socket.
set -uo pipefail

CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_STOCK="${CADDY_STOCK:-/etc/caddy/Caddyfile.stock}"
LIVEKIT_DIR="${LIVEKIT_DIR:-/home/ubuntu/apps/livekit}"
LIVEKIT_YAML="${LIVEKIT_YAML:-${LIVEKIT_DIR}/livekit.yaml}"
LIVEKIT_STOCK="${LIVEKIT_STOCK:-${LIVEKIT_YAML}.stock}"
FW_RULE=(! -i lo -p tcp --dport 5349 -j DROP)   # the plaintext-5349 guard rule

log()  { echo "[rollback] $*"; }
die()  { echo "[rollback] FATAL: $*" >&2; exit 1; }
dc()   { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
[ "$(id -u)" -eq 0 ] || die "run as root (systemctl / docker / iptables)"

# --- 1. Stop HAProxy, free public :443 ------------------------------------------------------
if systemctl is-active --quiet haproxy; then
  log "stopping haproxy (frees :443)"
  systemctl stop haproxy || die "could not stop haproxy — :443 still muxed, ABORT (fix by hand)"
else
  log "haproxy already stopped"
fi

# --- 2. Restore Caddy to public :443 --------------------------------------------------------
[ -s "$CADDY_STOCK" ] || die "no $CADDY_STOCK to restore — cannot put Caddy back on :443 by hand-guessing; ABORT"
if ! cmp -s "$CADDY_STOCK" "$CADDYFILE"; then
  log "restoring stock Caddyfile"
  cp "$CADDY_STOCK" "$CADDYFILE"
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || die "stock Caddyfile fails validation — NOT restarting Caddy; ABORT"
log "restarting caddy (back on public :443)"
systemctl restart caddy || die "caddy restart failed — :443 may be down; investigate NOW"

# --- 3. Restore LiveKit to TLS-on-5349 (its own cert) --------------------------------------
if [ -s "$LIVEKIT_STOCK" ]; then
  if ! cmp -s "$LIVEKIT_STOCK" "$LIVEKIT_YAML"; then
    log "restoring stock livekit.yaml (TLS on 5349, own cert)"
    cp "$LIVEKIT_STOCK" "$LIVEKIT_YAML"
  fi
  log "restarting livekit"
  dc -f "${LIVEKIT_DIR}/docker-compose.yml" restart livekit \
    || die "livekit restart failed — 5349 state UNKNOWN; do NOT reopen the firewall until TLS is confirmed on 5349"
else
  log "WARN: no $LIVEKIT_STOCK — assuming livekit.yaml was never flipped; leaving it as-is"
fi

# --- 3b. HARD GATE before reopening the firewall: 5349 must speak TLS, not plaintext --------
# This is the invariant the whole rollback exists to protect. openssl handshake must SUCCEED
# on localhost:5349; if it doesn't, LiveKit is still plaintext (or down) and we must NOT
# reopen the public firewall.
if ! timeout 8 openssl s_client -connect 127.0.0.1:5349 -servername turn.enspyr.co </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
  die "5349 does NOT present a TLS certificate after livekit restore — it may still be plaintext or down. REFUSING to reopen the public :5349 firewall (that would expose plaintext TURN). Fix livekit.yaml/restart by hand, then re-run."
fi
log "confirmed: 5349 presents TLS — safe to reopen firewall"

# --- 4. Reopen public :5349 (LAST — TLS is confirmed back) ----------------------------------
if iptables -C INPUT "${FW_RULE[@]}" 2>/dev/null; then
  log "removing the plaintext-5349 DROP rule (reopening public 5349, now TLS)"
  iptables -D INPUT "${FW_RULE[@]}" || die "failed to remove firewall DROP — 5349 still localhost-only (SAFE state, but not fully rolled back); fix by hand"
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
else
  log "no plaintext-5349 DROP rule present (already open or never added)"
fi

# Consume the .stock backups — they've served their purpose. Leaving them would make the
# NEXT cutover.sh abort on its "a prior cutover is in progress" guard forever.
rm -f "$CADDY_STOCK" "$LIVEKIT_STOCK"

log "ROLLBACK COMPLETE — Caddy on :443, LiveKit TLS on 5349, HAProxy stopped, firewall open, .stock consumed."
log "Verify: curl -sI https://chat.enspyr.co | head -1 ; and re-run b3_relay_probe (turns:443 will be UNREACHABLE again — expected pre-mux)."
