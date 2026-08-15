#!/usr/bin/env bash
# rollback.sh — undo cutover.sh: give public :443 back to Caddy.
#
# Under the passthrough shape (task #8) this is a TWO-artifact restore, and the difference is
# not cosmetic. The old version restored FOUR coupled artifacts in a security-critical order,
# because the cutover had put LiveKit on plaintext TURN: rollback had to restore TLS on :5349
# BEFORE reopening the firewall, behind a hard gate that refused to proceed if it could not
# confirm it. That gate — the most dangerous step in the whole runbook, the one that could
# expose plaintext TURN to the internet if it ever passed vacuously — no longer exists, because
# the plaintext it guarded no longer exists. LiveKit is never touched by the cutover at all.
#
# What is left:
#   1. HAProxy   — stop + DISABLE, freeing public :443
#   2. Caddy     — restore Caddyfile.stock, back onto public :443
#   3. firewall  — reopen :8443 (Caddy's loopback-only HTTPS port while muxed)
#
# Idempotent + safe to run STANDALONE: every step checks current state before acting.
set -uo pipefail

CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_STOCK="${CADDY_STOCK:-/etc/caddy/Caddyfile.stock}"
FW_PORTS=(8443)   # the loopback-only guard port cutover added (v4+v6): Caddy's HTTPS while muxed

log()  { echo "[rollback] $*"; }
die()  { echo "[rollback] FATAL: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root (systemctl / iptables)"

drop_present() {
  local fam port
  for fam in iptables ip6tables; do
    command -v "$fam" >/dev/null 2>&1 || continue
    for port in "${FW_PORTS[@]}"; do
      "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null && return 0
    done
  done
  return 1
}

# --- 0. IS THERE ANYTHING TO ROLL BACK TO? (Tesla, HIGH) -------------------------------------
# This must precede step 1, because step 1 frees :443. The old order was: disable HAProxy
# FIRST, then discover there is no Caddyfile.stock and die — which on a migrated box (where
# migrate-to-passthrough.sh consumed the stock file, and Caddy is on :8443 with no config to
# put it back on :443) means the RUNBOOK's own "rollback anytime" instruction takes the LIVE
# front door down and leaves it down. Same wrong-entrypoint class Carnot found in cutover.sh,
# at the other end of the pair: mutate first, discover the wrong universe second.
#
# So: if HAProxy currently owns :443 and there is no stock Caddyfile to restore, REFUSE —
# before touching anything. Taking :443 down with nothing to hand it to is strictly worse
# than the muxed state we were asked to leave.
# Keyed off PORT OWNERSHIP, not `systemctl is-active` (Carnot): the invariant is "something is
# serving :443 and there is nothing to hand it to", and a process can own the socket while the
# unit reads inactive — supervised outside systemd, mid-restart, or a lagging unit state. Gating
# the guard on is-active would let exactly those cases fall through into the stop/disable.
if [ ! -s "$CADDY_STOCK" ]; then
  # Any owner that is not Caddy — or no owner at all — means stopping HAProxy cannot improve
  # things and may make them worse, and there is nothing to restore either way (Carnot round 4:
  # the guard named port ownership but protected only one owner).
  _own="$(ss -tlnpH 'sport = :443' 2>/dev/null | grep -oE '"[^"]+"' | head -1 | tr -d '"')"
  if [ "$_own" != "caddy" ]; then
    die "HAProxy owns :443 but there is no $CADDY_STOCK to restore Caddy from.

(:443 owner is '"'"'${_own:-<unbound>}'"'"'.) This is the post-migrate-to-passthrough shape: the stock
Caddyfile was consumed on a successful migration, so there is nothing to roll :443 back TO. Stopping HAProxy here would
free :443 with no owner and leave chat, signaling and TURN dark.

REFUSING. If you genuinely want to leave the mux, restore a public-:443 Caddyfile by hand
(drop the global 'https_port 8443' + the :8443 proxy_protocol listener from
/etc/caddy/Caddyfile), verify 'caddy validate', THEN re-run this script."
  fi
fi

# --- 1. Stop AND DISABLE HAProxy, free public :443 ------------------------------------------
# DISABLE (not just stop): an enabled haproxy would restart on the next reboot and reclaim :443
# while Caddy is also restored to public :443 → double-bind / outage after an "apparently
# successful" rollback (Carnot P1).
if systemctl is-active --quiet haproxy || systemctl is-enabled --quiet haproxy 2>/dev/null; then
  log "stopping + disabling haproxy (frees :443, won't reclaim on reboot)"
  systemctl disable --now haproxy 2>/dev/null || die "could not stop/disable haproxy — :443 still muxed, ABORT (fix by hand)"
else
  log "haproxy already stopped + disabled"
fi

# --- 2. Restore Caddy to public :443 --------------------------------------------------------
# Idempotency (Tesla P1): a SUCCESSFUL rollback rm's the .stock file, so a 2nd standalone run
# finds no CADDY_STOCK. That is NOT an error IF the box is already stock — detect it and no-op
# success. Only die if the stock is missing AND we are in a genuinely-broken half-state.
#
# The stock invariant is SHORTER than it was, and only because there is less state: haproxy
# gone, Caddy on :443, no residual guard DROP. It no longer has to prove anything about
# LiveKit, because the cutover never changed LiveKit. (The old check had to include "LiveKit
# presents TLS on 5349" — without it a cosmetically-stock box could report COMPLETE while
# leaving public plaintext TURN.)
if [ ! -s "$CADDY_STOCK" ]; then
  if ! systemctl is-active --quiet haproxy && ! systemctl is-enabled --quiet haproxy 2>/dev/null \
     && systemctl is-active --quiet caddy && [ -n "$(ss -tlnH 'sport = :443' 2>/dev/null)" ] \
     && ! drop_present; then
    log "no $CADDY_STOCK and the stock invariant holds (haproxy gone, Caddy on :443, no guard DROP) — nothing to do"
    exit 0
  fi
  die "no $CADDY_STOCK to restore and the box is NOT stock (haproxy present? residual DROP? :443 unbound?) — cannot finish; fix by hand"
fi
if ! cmp -s "$CADDY_STOCK" "$CADDYFILE"; then
  log "restoring stock Caddyfile"
  # CHECK THE cp (Tesla): there is no `set -e` here, and a failed copy leaves Caddyfile.mux in
  # place — which `caddy validate` then PASSES, and `systemctl restart caddy` then serves happily
  # on :8443, with HAProxy already stopped. The script would log ROLLBACK COMPLETE over an
  # unbound front door. Verify the copy landed, byte for byte, before trusting anything after it.
  cp "$CADDY_STOCK" "$CADDYFILE" || die "failed to copy $CADDY_STOCK over $CADDYFILE — :443 is currently UNBOUND (haproxy is stopped). Restore it by hand NOW."
  cmp -s "$CADDY_STOCK" "$CADDYFILE" || die "the restored $CADDYFILE does not match $CADDY_STOCK — refusing to restart Caddy onto an unverified config while :443 is unbound."
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 \
  || die "stock Caddyfile fails validation — NOT restarting Caddy; fix it by hand (:443 is currently unbound)"
log "restarting caddy (back on public :443)"
systemctl restart caddy || die "caddy restart failed — :443 may be down; investigate NOW"
# And VERIFY it actually took :443 (Tesla): "restart succeeded" only says the unit started. If
# the restored config still puts Caddy on :8443, the unit is happily green and the front door is
# unbound — the exact "verifies the parchment, not the door" failure. Bounded poll, then assert.
for _ in $(seq 1 30); do [ -n "$(ss -tlnpH 'sport = :443' 2>/dev/null | grep caddy)" ] && break; sleep 1; done
[ -n "$(ss -tlnpH 'sport = :443' 2>/dev/null | grep caddy)" ] \
  || die "caddy restarted but does NOT own :443 (owner: '$(ss -tlnpH 'sport = :443' 2>/dev/null | grep -oE '\"[^\"]+\"' | head -1 | tr -d '\"')'). The front door is not restored — do NOT walk away."

# --- 3. Reopen :8443 — BOTH families ---------------------------------------------------------
# cutover added the DROP on iptables AND ip6tables; remove both so rollback is symmetric.
# Note this is no longer ordered-last-for-safety: with no plaintext anywhere, reopening :8443
# early or late is equally harmless. It is last only because it is tidy.
removed_any=0
for fam in iptables ip6tables; do
  command -v "$fam" >/dev/null 2>&1 || continue
  for port in "${FW_PORTS[@]}"; do
    if "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null; then
      log "removing the loopback DROP for :$port ($fam)"
      "$fam" -D INPUT ! -i lo -p tcp --dport "$port" -j DROP \
        || die "failed to remove $fam DROP for :$port — still localhost-only (SAFE, but incomplete); fix by hand"
      removed_any=1
    fi
  done
done
if [ "$removed_any" = 1 ]; then
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 \
      || log "WARN: netfilter-persistent save failed — the DROP is removed at runtime but a reboot may RESTORE the persisted DROP"
  fi
else
  log "no guard DROP present on either family (already open or never added)"
fi

# Purge any residue from the OLD external_tls shape, if this box ever ran it. Inert here, but a
# stale timer poking a stopped haproxy every tick is confusing archaeology for the next operator.
systemctl disable --now haproxy-cert-sync.timer 2>/dev/null || true
rm -f /usr/local/bin/haproxy-cert-sync.sh /usr/local/sbin/haproxy-cert-sync.sh \
      /etc/systemd/system/haproxy-cert-sync.service \
      /etc/systemd/system/haproxy-cert-sync.timer
systemctl daemon-reload 2>/dev/null || true

# Consume the .stock backup — leaving it would make the NEXT cutover.sh abort on its
# "a prior cutover is in progress" guard forever.
rm -f "$CADDY_STOCK"

log "ROLLBACK COMPLETE — Caddy on :443, HAProxy stopped+disabled, :8443 reopened, .stock consumed."
log "LiveKit was never touched: it is still serving its own TLS on :5349, exactly as before."
log "Verify: curl -sI https://<chat-domain> | head -1 ; b3_relay_probe will show turns:443 UNREACHABLE again (expected pre-mux state)."
