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
FW_PORTS=(5349 8443)   # the loopback-only guard ports cutover added (v4+v6): plaintext TURN + Caddy HTTPS

log()  { echo "[rollback] $*"; }
die()  { echo "[rollback] FATAL: $*" >&2; exit 1; }
dc()   { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
[ "$(id -u)" -eq 0 ] || die "run as root (systemctl / docker / iptables)"

# --- 1. Stop AND DISABLE HAProxy, free public :443 ------------------------------------------
# DISABLE (not just stop): an enabled haproxy would restart on the next reboot and reclaim :443
# while Caddy is also restored to public :443 → double-bind / outage after an "apparently
# successful" rollback (Carnot P1). Mask-equivalent: disable + stop returns to stock (Caddy owns
# :443, no haproxy).
if systemctl is-active --quiet haproxy || systemctl is-enabled --quiet haproxy 2>/dev/null; then
  log "stopping + disabling haproxy (frees :443, won't reclaim on reboot)"
  systemctl disable --now haproxy 2>/dev/null || die "could not stop/disable haproxy — :443 still muxed, ABORT (fix by hand)"
else
  log "haproxy already stopped + disabled"
fi
# Disable the cert-sync timer too — it's meaningless once HAProxy is gone, and leaving it enabled
# would keep poking a nonexistent haproxy on every tick.
systemctl disable --now haproxy-cert-sync.timer 2>/dev/null || true

# --- 2. Restore Caddy to public :443 --------------------------------------------------------
# Idempotency (Tesla P1): a SUCCESSFUL rollback rm's the .stock files, so a 2nd standalone run
# finds no CADDY_STOCK. That is NOT an error IF the box is already stock — detect it (haproxy
# gone + Caddy active + something on :443) and no-op success, rather than die. Only die if the
# stock is missing AND we're in a genuinely-broken half-state.
if [ ! -s "$CADDY_STOCK" ]; then
  # FULL stock invariant (Tesla P1): "already rolled back → exit clean" must prove EVERY artifact,
  # not just Caddy-on-:443 — else a COSMETICALLY-stock box (Caddy on :443 but livekit STILL
  # plaintext + the DROP already removed) would report COMPLETE while leaving PUBLIC PLAINTEXT
  # TURN. Require: haproxy gone, Caddy on :443, LiveKit presents TLS on :5349, and no guard DROP.
  lk_tls=0
  timeout 8 openssl s_client -connect 127.0.0.1:5349 -servername turn.enspyr.co </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE" && lk_tls=1
  drop_present=0
  for fam in iptables ip6tables; do
    command -v "$fam" >/dev/null 2>&1 || continue
    for port in "${FW_PORTS[@]}"; do
      "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null && drop_present=1
    done
  done
  if ! systemctl is-active --quiet haproxy && ! systemctl is-enabled --quiet haproxy 2>/dev/null \
     && systemctl is-active --quiet caddy && [ -n "$(ss -tlnH 'sport = :443' 2>/dev/null)" ] \
     && [ "$lk_tls" = 1 ] && [ "$drop_present" = 0 ]; then
    log "no $CADDY_STOCK and the FULL stock invariant holds (haproxy gone, Caddy on :443, LiveKit TLS on 5349, no guard DROP) — nothing to roll back; exiting clean."
    exit 0
  fi
  die "no $CADDY_STOCK to restore and the box is NOT fully stock (haproxy present? livekit still plaintext? residual DROP?) — cannot finish rollback safely without the backup; fix by hand. ABORT."
fi
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
# Bounded readiness poll FIRST (Tesla P1): share cutover's pattern so a slow livekit restart
# doesn't false-red the gate on the first run. Wait up to 30s for Running + 5349 listening.
for _ in $(seq 1 30); do
  [ "$(docker inspect livekit --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
    && timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/5349" 2>/dev/null && break
  sleep 1
done
# This is the invariant the whole rollback exists to protect. openssl handshake must SUCCEED
# on localhost:5349; if it doesn't, LiveKit is still plaintext (or down) and we must NOT
# reopen the public firewall.
if ! timeout 8 openssl s_client -connect 127.0.0.1:5349 -servername turn.enspyr.co </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
  die "5349 does NOT present a TLS certificate after livekit restore — it may still be plaintext or down. REFUSING to reopen the public :5349 firewall (that would expose plaintext TURN). Fix livekit.yaml/restart by hand, then re-run."
fi
log "confirmed: 5349 presents TLS — safe to reopen firewall"

# --- 4. Reopen public :5349 + :8443 (LAST — TLS is confirmed back) — BOTH families -----------
# cutover added the DROP on iptables AND ip6tables for every guard port; remove them all so
# rollback is symmetric. (:5349 reopens now-TLS TURN; :8443 was Caddy's loopback-only HTTPS.)
removed_any=0
for fam in iptables ip6tables; do
  command -v "$fam" >/dev/null 2>&1 || continue
  for port in "${FW_PORTS[@]}"; do
    if "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null; then
      log "removing the loopback DROP for :$port ($fam)"
      "$fam" -D INPUT ! -i lo -p tcp --dport "$port" -j DROP || die "failed to remove $fam DROP for :$port — still localhost-only (SAFE, but not fully rolled back); fix by hand"
      removed_any=1
    fi
  done
done
if [ "$removed_any" = 1 ]; then
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 \
      || log "WARN: netfilter-persistent save failed — the DROP is removed at runtime but a reboot may RESTORE the persisted DROP (5349 would stay closed; SAFE direction, but rollback is incomplete). Re-run 'netfilter-persistent save' by hand."
  fi
else
  log "no plaintext-5349 DROP rule present on either family (already open or never added)"
fi

# Purge the cutover-installed cert-sync artifacts (Kelvin P2: a rollback should restore the
# filesystem, not just process state). These are purely ours + inert once the timer is disabled;
# removing them leaves no confusing residue. (We do NOT remove the haproxy PACKAGE or its
# /etc/haproxy/haproxy.cfg — the package predates nothing here but purging it is out of a
# rollback's scope; it's disabled + inert, and a re-cutover reuses it.)
rm -f /usr/local/bin/haproxy-cert-sync.sh \
      /etc/systemd/system/haproxy-cert-sync.service \
      /etc/systemd/system/haproxy-cert-sync.timer \
      "/etc/haproxy/certs/turn.enspyr.co.pem" "/etc/haproxy/certs/turn.enspyr.co.pem.needs-reload"
systemctl daemon-reload 2>/dev/null || true

# Consume the .stock backups — they've served their purpose. Leaving them would make the
# NEXT cutover.sh abort on its "a prior cutover is in progress" guard forever.
rm -f "$CADDY_STOCK" "$LIVEKIT_STOCK"

log "ROLLBACK COMPLETE — Caddy on :443, LiveKit TLS on 5349, HAProxy stopped+disabled, firewall open, cert-sync purged, .stock consumed."
log "Verify: curl -sI https://chat.enspyr.co | head -1 ; and re-run b3_relay_probe (turns:443 will be UNREACHABLE again — expected pre-mux)."
