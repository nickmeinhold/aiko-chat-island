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
# Loopback-only guard ports, applied to BOTH families (Carnot/Tesla: an iptables-only rule does
# nothing for IPv6). 5349 = LiveKit plaintext TURN (external_tls); 8443 = Caddy HTTPS after it
# moves off :443 (only HAProxy on 127.0.0.1 should reach it — Carnot P0, and not relying on the
# OCI security-list alone). ! -i lo = block every non-loopback ingress to that tcp port.
FW_PORTS=(5349 8443)

log()  { echo "[cutover] $*"; }
die()  { echo "[cutover] ABORT (no mutation past this point): $*" >&2; exit 1; }
roll() { echo "[cutover] !!! VERIFY FAILED: $* — AUTO-ROLLBACK !!!" >&2; bash "${HERE}/rollback.sh"; exit 1; }
# docker compose v2 (plugin) or v1 (standalone) — don't silently fail on a v1 box.
dc()   { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
# Apply the DROP for every guard port on BOTH iptables (v4) and ip6tables (v6), idempotently,
# and VERIFY each rule is present afterward (fail-closed: a silent add-failure must not let the
# cutover proceed). Returns non-zero if any (family × port) can't be confirmed.
fw_block_ports() {
  local fam port
  for fam in iptables ip6tables; do
    command -v "$fam" >/dev/null 2>&1 || { echo "[cutover] $fam missing — cannot guard the loopback-only ports on that family"; return 1; }
    for port in "${FW_PORTS[@]}"; do
      "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null \
        || "$fam" -I INPUT 1 ! -i lo -p tcp --dport "$port" -j DROP || return 1
      "$fam" -C INPUT ! -i lo -p tcp --dport "$port" -j DROP 2>/dev/null \
        || { echo "[cutover] $fam DROP for :$port not present after add"; return 1; }
    done
  done
}
port_listening() { [ -n "$(ss -tlnH "sport = :$1" 2>/dev/null)" ]; }
# REHEARSAL-ONLY checkpoint stop (task #6). The four machines must be provable at their
# INTERMEDIATE states — "reboot at CP2" and "rollback from CP3" are the tests that catch the
# boot-correctness and 4-artifact-restore bugs a diff-read cannot (INV-3 / INV-6). Exiting
# here leaves the system deliberately mid-cutover, so it is DOUBLE-gated: inert unless BOTH
# REHEARSAL=1 and CUTOVER_STOP_AFTER name the checkpoint. Exit 99 = "stopped on purpose",
# distinct from any real failure code.
ckpt() {
  [ "${REHEARSAL:-0}" = "1" ] || return 0
  [ "${CUTOVER_STOP_AFTER:-}" = "$1" ] || return 0
  log "REHEARSAL: stopping after checkpoint '$1' — state left intentionally mid-cutover."
  exit 99
}
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

# INV-1 depends on the :5349 DROP sitting in the path packets actually take. That holds ONLY
# if LiveKit is HOST-networked (binds *:5349 on the host → INPUT chain). If it were a
# bridge/published port, packets would be DNATed via DOCKER-USER/FORWARD and the INPUT DROP
# would never see them → public plaintext TURN (Carnot+Tesla P0). Assert host networking rather
# than assume it; die fail-closed if the topology ever changes.
LK_NETMODE="$(docker inspect livekit --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || true)"
[ "$LK_NETMODE" = "host" ] || die "livekit NetworkMode is '${LK_NETMODE:-unknown}', not 'host' — the INPUT :5349 DROP would NOT be in a bridge/published port's DNAT path. Refusing (would risk public plaintext TURN). Guard 5349 in DOCKER-USER or bind loopback instead."
# Persistence must be REQUIRED, not best-effort — a runtime-only DROP is cleared by a reboot,
# reopening public plaintext :5349 while livekit.yaml stays external_tls (Carnot P0). netfilter-
# persistent must exist so the rule survives; die if absent rather than ship a reboot time-bomb.
command -v netfilter-persistent >/dev/null 2>&1 || die "netfilter-persistent absent — the :5349 DROP would not survive a reboot (reopening public plaintext TURN). Install iptables-persistent first."
# openssl is load-bearing for the plaintext-flip verify (Tesla P1): the 2.2 check treats "no TLS
# cert presented" as proof of plaintext, so a MISSING openssl would make that check pass
# vacuously (fail-OPEN on the security-critical flip). Require it up front so the only reason for
# "no cert" is genuine plaintext, not a missing tool.
command -v openssl >/dev/null 2>&1 || die "openssl absent — the plaintext-flip verify would fail OPEN without it"
command -v ss >/dev/null 2>&1 || die "ss absent — needed for the listener/owner checks"

command -v haproxy >/dev/null 2>&1 || { log "installing haproxy"; apt-get install -y haproxy >/dev/null || die "haproxy install failed"; }
id haproxy >/dev/null 2>&1 || die "haproxy user missing after install"
# DISABLE, not just stop (Tesla P0): apt's postinst ENABLES+starts haproxy. With the mux config
# about to be installed to /etc/haproxy/haproxy.cfg while Caddy still owns public :443, an
# enabled haproxy would START on any reboot in the Phase-0→2.4 window and double-bind :443. Keep
# it disabled until 2.4 flips it on intentionally, so no reboot mid-cutover can race the acceptor.
systemctl disable --now haproxy 2>/dev/null || true

# Build + validate the turn PEM from Caddy's store (cert-sync is the sole writer). haproxy
# not active yet → it just stages the PEM.
log "building turn PEM from Caddy store"
TURN_DOMAIN="$TURN_DOMAIN" bash "$CERT_SYNC" || die "cert-sync could not build the turn PEM (is Caddy managing $TURN_DOMAIN?)"
[ -s "/etc/haproxy/certs/${TURN_DOMAIN}.pem" ] || die "turn PEM absent after cert-sync"

# Install the mux config into haproxy's canonical path and config-check it.
install -D -m 0644 "$HAPROXY_CFG" /etc/haproxy/haproxy.cfg
haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null 2>&1 || die "haproxy -c failed on /etc/haproxy/haproxy.cfg"
caddy validate --config "$CADDY_MUX" --adapter caddyfile >/dev/null 2>&1 || die "Caddyfile.mux fails caddy validate"
# Honest scope (Tesla P2): Phase 0 HAS mutated some prep state — haproxy installed+disabled, the
# mux haproxy.cfg + turn PEM staged. None of it is live-serving (haproxy is stopped+disabled,
# Caddy still owns :443), so an abort here needs no rollback of live traffic — but a manual
# `systemctl start haproxy` before 2.3 WOULD double-bind :443. The disabled unit prevents that on
# reboot; don't hand-start it.
log "PHASE 0 OK — preconditions clear; prep staged (haproxy installed+DISABLED, cfg+PEM staged), no live change yet."

# ============================ PHASE 1 — stage backups (FAIL-CLOSED) ==========================
# The backups ARE the rollback's trusted source of truth — an unchecked cp that silently fails
# leaves rollback with no stock artifact to restore (Carnot P1). Verify each copy byte-for-byte
# before proceeding to any mutation.
log "staging .stock backups (the rollback set)"
cp "$CADDYFILE"    "$CADDY_STOCK"    || die "failed to back up $CADDYFILE"
cp "$LIVEKIT_YAML" "$LIVEKIT_STOCK"  || die "failed to back up $LIVEKIT_YAML"
cmp -s "$CADDYFILE"    "$CADDY_STOCK"    || die "Caddyfile backup verification (cmp) failed"
cmp -s "$LIVEKIT_YAML" "$LIVEKIT_STOCK"  || die "livekit.yaml backup verification (cmp) failed"
FW_SNAP="$(mktemp /tmp/cutover-iptables.XXXXXX)"; iptables-save > "$FW_SNAP" 2>/dev/null || true
log "firewall snapshot at $FW_SNAP"

# ============================ PHASE 2 — the sequenced cutover =================================
# --- 2.1 INV-1: firewall :5349 + :8443 (both families, verified) BEFORE any plaintext exists --
# Once this mutates iptables, a bare `die` would STRAND a partial DROP on production's currently-
# working public :5349 TLS (Carnot P0). So every failure from here uses roll() — rollback.sh
# removes the DROPs (both ports, both families) and restores the untouched .stock state.
log "2.1 firewalling public :5349 + :8443 (v4+v6, loopback-only) BEFORE the plaintext flip"
fw_block_ports || roll "could not confirm the loopback DROP on both families — unwinding (rollback removes any partial rule)"
# Persist fail-closed: a runtime-only rule is cleared by a reboot (Carnot P0). If save fails,
# ROLL BACK — do not leave a reboot time-bomb (livekit plaintext + a volatile-only DROP).
netfilter-persistent save >/dev/null 2>&1 || roll "netfilter-persistent save FAILED — the DROPs would not survive a reboot; unwinding"
ckpt CP1

# --- 2.2 flip LiveKit to external_tls (plaintext on 5349, now firewalled) --------------------
# Canonical, turn:-SCOPED YAML edit (Kelvin+Carnot+Tesla — all three flagged the unbounded sed):
# an awk pass bounded to the top-level `turn:` mapping deletes any cert_file/key_file/external_tls
# there and inserts one `external_tls: true` after tls_port — so it cannot collaterally delete a
# same-named 2-space key in ANOTHER section, and a pre-existing `external_tls: false` (YAML
# last-key-wins) can't survive. (yq deliberately NOT used: mikefarah vs python-yq flavors differ
# and a wrong-flavor call would spuriously roll back; pyyaml reorders + strips comments. The file
# is machine-managed and the liveness gate below is the real backstop.)
log "2.2 flipping livekit.yaml → external_tls:true (drop cert_file/key_file, turn-scoped), restart"
awk '
  /^[^[:space:]]/ { in_turn = ($0 ~ /^turn:/) }
  in_turn && /^[[:space:]]+(cert_file|key_file|external_tls):/ { next }
  { print }
  in_turn && /^[[:space:]]+tls_port:/ {
    match($0, /^[[:space:]]+/); print substr($0, 1, RLENGTH) "external_tls: true"   # copy tls_port indent (Tesla P2)
  }
' "$LIVEKIT_YAML" > "${LIVEKIT_YAML}.new" && mv "${LIVEKIT_YAML}.new" "$LIVEKIT_YAML" \
  || roll "awk edit of livekit.yaml failed"
grep -q '^  external_tls: true' "$LIVEKIT_YAML" || roll "livekit.yaml external_tls edit did not take"
dc -f "${LIVEKIT_DIR}/docker-compose.yml" restart livekit || roll "livekit restart failed"
# LIVENESS BEFORE PLAINTEXT (Tesla P0 — the false-green fix): a broken config bootloops LiveKit,
# and a DEAD socket also fails a TLS handshake — so "no cert seen" is NOT proof of plaintext.
# Require, in order: (a) container RUNNING, (b) 5349 LISTENING, (c) livekit holds it, (d) TLS
# fails. BOUNDED POLL, not a fixed `sleep 4` (Kelvin): wait up to 30s for (a)+(b) rather than
# guessing the restart duration; then assert the rest.
for _ in $(seq 1 30); do
  [ "$(docker inspect livekit --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
    && timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/5349" 2>/dev/null && break
  sleep 1
done
[ "$(docker inspect livekit --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
  || roll "livekit container is not Running 30s after the external_tls edit (bootloop? bad config)"
timeout 6 bash -c "exec 3<>/dev/tcp/127.0.0.1/5349" 2>/dev/null \
  || roll "nothing is LISTENING on 127.0.0.1:5349 30s after restart — livekit did not bind (a TLS-fail here would be a FALSE green)"
# Assert LIVEKIT itself holds :5349 — not just "something plaintext" (Tesla P2: a stray listener
# would also pass Running+LISTENING+TLS-fail). The positive "it's really TURN" proof is the
# off-box b3 probe (RUNBOOK acceptance); this bounds the on-box residue cheaply.
ss -tlnpH 'sport = :5349' 2>/dev/null | grep -q 'livekit-server' \
  || roll "the process listening on :5349 is not livekit-server — refusing (external_tls state unproven)"
if timeout 6 openssl s_client -connect 127.0.0.1:5349 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
  roll "5349 still presents TLS after external_tls flip — livekit did not apply external_tls"
fi
log "2.2 confirmed: livekit-server RUNNING + holds :5349 LISTENING + plaintext (no TLS) + firewalled"
ckpt CP2

# --- 2.3 move Caddy off public :443 → loopback:8443 -----------------------------------------
log "2.3 installing Caddyfile.mux, enabling haproxy, reloading caddy (releases public :443)"
cp "$CADDY_MUX" "$CADDYFILE"
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || roll "installed Caddyfile.mux failed validation"
# ENABLE haproxy HERE — BEFORE the live Caddy reload (Carnot reboot-window fix). Once the mux
# Caddyfile is on disk (Caddy→:8443) AND haproxy is enabled, the PERSISTED state is boot-correct:
# any reboot from this point boots Caddy on :8443 + haproxy on :443, no conflict, no unbound :443.
# (If it were enabled only at 2.4 as before, a reboot in the 2.3-reload→2.4-start window would boot
# Caddy-on-8443 + haproxy-disabled → :443 unbound.) Hard-roll on failure (Tesla). The only residual
# window is the ~100ms cp→enable above, before any LIVE change (Caddy still serving :443 in-memory).
systemctl enable haproxy >/dev/null 2>&1 || roll "could not enable haproxy unit — a reboot could leave :443 unbound; refusing to stand up a non-persistent mux"
# ---- INV-2: :443 dark window OPENS here ----
DARK_START=$(date +%s%3N)
systemctl reload caddy || roll "caddy reload failed"
# Wait for Caddy to actually RELEASE public :443 before HAProxy grabs it — a graceful reload
# can hold the old listener briefly, and starting haproxy into a still-bound :443 = EADDRINUSE
# → a spurious rollback. Bounded (5s); this wait is PART OF the measured dark window (honest).
for _ in $(seq 1 50); do port_listening 443 || break; sleep 0.1; done
port_listening 443 && roll "Caddy did not release :443 within 5s of reload"
ckpt CP3

# --- 2.4 start HAProxy on :443 (closes the dark window) — back-to-back with 2.3 -------------
log "2.4 starting haproxy on :443"
# (haproxy was already ENABLED in 2.3 so the persisted state is boot-correct; here we just start
# the live process to bind :443 now.)
systemctl start haproxy || roll "haproxy failed to start"
DARK_END=$(date +%s%3N)
log "INV-2: :443 dark window was $((DARK_END - DARK_START)) ms"
# Preflight the internal terminator actually bound (Carnot dead-backend): :8444 must listen.
port_listening 8444 || roll "fe_turn_terminate did not bind :8444 (cert problem?)"
port_listening 443  || roll ":443 not bound after haproxy start"
ckpt CP4

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
# VERIFIED enable (Carnot: the cert time-bomb is a named landmine, not a soft WARN). If the
# timer can't be made active, the mux WILL serve a stale cert in ~89d — roll back rather than
# stand up a cutover that silently rots.
systemctl enable --now haproxy-cert-sync.timer >/dev/null 2>&1 || roll "could not enable haproxy-cert-sync.timer — the cert would rot in ~89d; refusing to leave a silent time-bomb"
systemctl is-active --quiet haproxy-cert-sync.timer || roll "haproxy-cert-sync.timer enabled but not active — cert renewal not guaranteed"

log "================================================================"
log "CUTOVER on-box verify GREEN. :443 dark window ${DARK_END:+$((DARK_END - DARK_START))ms}."
log "FINAL ACCEPTANCE (run from OFF-BOX now): b3_relay_probe against turns:443 must flip"
log "  UNREACHABLE → ALLOCATED, with RFC1918/CGNAT still 403. If it FAILS → run rollback.sh."
log "Rollback anytime: sudo bash ${HERE}/rollback.sh"
log "================================================================"
