#!/usr/bin/env bash
# cutover.sh — the ONE irreversible step: hand public :443 from Caddy to the HAProxy TURN mux,
# as a fail-closed, 4-artifact, auto-rollback-on-red sequenced state machine.
#
# This replaces the cage-match's P0 "single fail-closed script (that doesn't exist)". The
# ordering enforces the two invariants the review demanded:
#   INV-1  plaintext TURN is NEVER reachable on a public socket → firewall :5349 to loopback
#          BEFORE flipping LiveKit to external_tls (plaintext).
#   INV-2  the :443 dark window (Caddy releases → HAProxy binds) is back-to-back and MEASURED,
#          never open longer than a bind handoff.
# Any verify failure → automatic rollback.sh (restores all four artifacts). Fail-closed:
# missing precondition → abort BEFORE any mutation.
#
# Preconditions the operator MUST have met first (the RUNBOOK's off-:443 proof):
#   - OFF443_PROVEN=1 in the env (you have staged the full chain on ALT ports and proven a
#     real TURNS allocation through it + the negative tests). cutover REFUSES to run without it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_STOCK="${CADDY_STOCK:-/etc/caddy/Caddyfile.stock}"
CADDY_MUX="${CADDY_MUX:-${HERE}/Caddyfile.mux}"
LIVEKIT_DIR="${LIVEKIT_DIR:-/home/ubuntu/apps/livekit}"
LIVEKIT_YAML="${LIVEKIT_YAML:-${LIVEKIT_DIR}/livekit.yaml}"
LIVEKIT_STOCK="${LIVEKIT_STOCK:-${LIVEKIT_YAML}.stock}"
HAPROXY_CFG="${HAPROXY_CFG:-${HERE}/haproxy.cfg}"
CERT_SYNC="${CERT_SYNC:-${HERE}/haproxy-cert-sync.sh}"
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
FW_RULE=(! -i lo -p tcp --dport 5349 -j DROP)

log()  { echo "[cutover] $*"; }
die()  { echo "[cutover] ABORT (no mutation past this point): $*" >&2; exit 1; }
roll() { echo "[cutover] !!! VERIFY FAILED: $* — AUTO-ROLLBACK !!!" >&2; bash "${HERE}/rollback.sh"; exit 1; }
# docker compose v2 (plugin) or v1 (standalone) — don't silently fail on a v1 box.
dc()   { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
port_listening() { [ -n "$(ss -tlnH "sport = :$1" 2>/dev/null)" ]; }
[ "$(id -u)" -eq 0 ] || die "run as root (systemctl / docker / iptables / apt)"

# ============================ PHASE 0 — preconditions (read-only) ============================
[ "${OFF443_PROVEN:-0}" = "1" ] || die "OFF443_PROVEN != 1 — prove the full chain on ALT ports first (RUNBOOK 'Build + PROVE OFF THE LIVE :443'). Refusing a blind live :443 takeover."
for f in "$CADDY_MUX" "$HAPROXY_CFG" "$CERT_SYNC" "${HERE}/rollback.sh"; do
  [ -s "$f" ] || die "missing required artifact: $f"
done
[ -s "$LIVEKIT_YAML" ] || die "no livekit.yaml at $LIVEKIT_YAML"
# A prior (aborted) cutover left .stock files → the current live files may be half-mutated.
# Refuse rather than clobber the true original: operator must rollback.sh first.
for s in "$CADDY_STOCK" "$LIVEKIT_STOCK"; do
  [ -e "$s" ] && die "$s already exists — a prior cutover is in progress/aborted. Run rollback.sh first, then retry."
done

command -v haproxy >/dev/null 2>&1 || { log "installing haproxy"; apt-get install -y haproxy >/dev/null || die "haproxy install failed"; }
id haproxy >/dev/null 2>&1 || die "haproxy user missing after install"
systemctl stop haproxy 2>/dev/null || true    # must NOT be bound to :443 yet (Caddy owns it)

# Build + validate the turn PEM from Caddy's store (cert-sync is the sole writer). haproxy
# not active yet → it just stages the PEM.
log "building turn PEM from Caddy store"
TURN_DOMAIN="$TURN_DOMAIN" bash "$CERT_SYNC" || die "cert-sync could not build the turn PEM (is Caddy managing $TURN_DOMAIN?)"
[ -s "/etc/haproxy/certs/${TURN_DOMAIN}.pem" ] || die "turn PEM absent after cert-sync"

# Install the mux config into haproxy's canonical path and config-check it.
install -D -m 0644 "$HAPROXY_CFG" /etc/haproxy/haproxy.cfg
haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null 2>&1 || die "haproxy -c failed on /etc/haproxy/haproxy.cfg"
caddy validate --config "$CADDY_MUX" --adapter caddyfile >/dev/null 2>&1 || die "Caddyfile.mux fails caddy validate"
log "PHASE 0 OK — preconditions clear, nothing mutated yet."

# ============================ PHASE 1 — stage backups ========================================
log "staging .stock backups (the rollback set)"
cp "$CADDYFILE"    "$CADDY_STOCK"
cp "$LIVEKIT_YAML" "$LIVEKIT_STOCK"
FW_SNAP="$(mktemp /tmp/cutover-iptables.XXXXXX)"; iptables-save > "$FW_SNAP" 2>/dev/null || true
log "firewall snapshot at $FW_SNAP"

# ============================ PHASE 2 — the sequenced cutover =================================
# --- 2.1 INV-1: firewall :5349 to loopback BEFORE any plaintext exists ----------------------
if ! iptables -C INPUT "${FW_RULE[@]}" 2>/dev/null; then
  log "2.1 firewalling public :5349 (loopback-only) BEFORE the plaintext flip"
  iptables -I INPUT 1 "${FW_RULE[@]}" || die "could not add :5349 DROP rule — refusing to flip LiveKit to plaintext with 5349 publicly reachable"
fi
command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true

# --- 2.2 flip LiveKit to external_tls (plaintext on 5349, now firewalled) --------------------
log "2.2 flipping livekit.yaml → external_tls:true (drop cert_file/key_file), restart"
sed -i -e '/^  cert_file:/d' -e '/^  key_file:/d' -e '/^  tls_port:/a\  external_tls: true' "$LIVEKIT_YAML"
grep -q '^  external_tls: true' "$LIVEKIT_YAML" || roll "livekit.yaml external_tls edit did not take"
dc -f "${LIVEKIT_DIR}/docker-compose.yml" restart livekit || roll "livekit restart failed"
# Give it a moment to bind, then confirm 5349 is now PLAINTEXT (TLS handshake must FAIL) — the
# proof the flip took. (It is firewalled to loopback, so we probe from localhost.)
sleep 4
if timeout 6 openssl s_client -connect 127.0.0.1:5349 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
  roll "5349 still presents TLS after external_tls flip — livekit did not apply external_tls"
fi
log "2.2 confirmed: 5349 is plaintext + firewalled to loopback"

# --- 2.3 move Caddy off public :443 → loopback:8443 -----------------------------------------
log "2.3 installing Caddyfile.mux, reloading caddy (releases public :443)"
cp "$CADDY_MUX" "$CADDYFILE"
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || roll "installed Caddyfile.mux failed validation"
# ---- INV-2: :443 dark window OPENS here ----
DARK_START=$(date +%s%3N)
systemctl reload caddy || roll "caddy reload failed"
# Wait for Caddy to actually RELEASE public :443 before HAProxy grabs it — a graceful reload
# can hold the old listener briefly, and starting haproxy into a still-bound :443 = EADDRINUSE
# → a spurious rollback. Bounded (5s); this wait is PART OF the measured dark window (honest).
for _ in $(seq 1 50); do port_listening 443 || break; sleep 0.1; done
port_listening 443 && roll "Caddy did not release :443 within 5s of reload"

# --- 2.4 start HAProxy on :443 (closes the dark window) — back-to-back with 2.3 -------------
log "2.4 starting haproxy on :443"
systemctl start haproxy || roll "haproxy failed to start"
DARK_END=$(date +%s%3N)
log "INV-2: :443 dark window was $((DARK_END - DARK_START)) ms"
# Preflight the internal terminator actually bound (Carnot dead-backend): :8444 must listen.
port_listening 8444 || roll "fe_turn_terminate did not bind :8444 (cert problem?)"
port_listening 443  || roll ":443 not bound after haproxy start"

# ============================ PHASE 3 — on-box verify (auto-rollback on any red) ==============
log "3 verifying (chat / livekit / turn-TLS on :443)"
sleep 2
# No -f: we verify the MUX ROUTES the SNI to Caddy and gets a response, not the HTTP status
# (chat's app / livekit's root may legitimately be non-2xx). A routing/passthrough failure is
# a curl transport error (exit != 0); an HTTP 404/401 is a successful route.
curl -sS -o /dev/null --max-time 10 https://chat.enspyr.co/ 2>/dev/null || roll "chat.enspyr.co not routing through the mux"
curl -sS -o /dev/null --max-time 10 https://livekit.enspyr.co/ 2>/dev/null || roll "livekit.enspyr.co not routing through the mux"
# turn:443 must now complete TLS and present the turn cert (HAProxy terminator). Full TURN
# allocation is the OFF-BOX b3 probe (acceptance gate below) — on-box we prove TLS terminates.
timeout 8 openssl s_client -connect "${TURN_DOMAIN}:443" -servername "$TURN_DOMAIN" </dev/null 2>/dev/null \
  | grep -q "BEGIN CERTIFICATE" || roll "turns:443 did not present a cert through HAProxy"

# ============================ PHASE 4 — wire the cert-sync timer ==============================
log "4 installing + enabling haproxy-cert-sync timer (the P1 cert-renewal fix)"
install -D -m 0755 "$CERT_SYNC" /usr/local/bin/haproxy-cert-sync.sh
install -D -m 0644 "${HERE}/haproxy-cert-sync.service" /etc/systemd/system/haproxy-cert-sync.service
install -D -m 0644 "${HERE}/haproxy-cert-sync.timer"   /etc/systemd/system/haproxy-cert-sync.timer
systemctl daemon-reload
systemctl enable --now haproxy-cert-sync.timer || log "WARN: could not enable cert-sync timer — enable it by hand or the cert will rot"

log "================================================================"
log "CUTOVER on-box verify GREEN. :443 dark window ${DARK_END:+$((DARK_END - DARK_START))ms}."
log "FINAL ACCEPTANCE (run from OFF-BOX now): b3_relay_probe against turns:443 must flip"
log "  UNREACHABLE → ALLOCATED, with RFC1918/CGNAT still 403. If it FAILS → run rollback.sh."
log "Rollback anytime: sudo bash ${HERE}/rollback.sh"
log "================================================================"
