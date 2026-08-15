#!/usr/bin/env bash
# cutover.sh — hand public :443 from Caddy to the HAProxy TURN mux, as a fail-closed,
# auto-rollback-on-red sequenced state machine. For a box where CADDY still owns :443.
# A box already running the mux migrates with migrate-to-passthrough.sh instead (backend swap,
# no dark window) — do not run this there.
#
# SHAPE: passthrough (task #8). HAProxy peeks SNI and forwards raw; LiveKit keeps its own TLS.
# This script used to move FOUR artifacts (Caddyfile + livekit.yaml + HAProxy + firewall) in a
# security-ordered sequence. It now moves TWO (Caddyfile + HAProxy) plus one firewall rule, and
# the ordering is convenience rather than a race against a window we opened ourselves. The
# invariant that drove all of it is gone, not mitigated:
#   INV-1 (deleted) plaintext TURN must never be publicly reachable — there is no plaintext.
#   INV-2 (kept)    the :443 dark window (Caddy releases → HAProxy binds) is back-to-back and
#                   MEASURED, never open longer than a bind handoff.
# Any verify failure → automatic rollback.sh. Fail-closed: missing precondition → abort BEFORE
# any mutation.
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
HAPROXY_TMPL="${HAPROXY_TMPL:-${HERE}/haproxy.cfg.tmpl}"
TURN_DOMAIN="${TURN_DOMAIN:?set TURN_DOMAIN (e.g. turn.enspyr.co)}"
# Loopback-only guard port, applied to BOTH families (Carnot/Tesla: an iptables-only rule does
# nothing for IPv6). 8443 = Caddy HTTPS after it moves off :443 (only HAProxy on 127.0.0.1
# should reach it — Carnot P0, and not relying on the OCI security-list alone).
# ! -i lo = block every non-loopback ingress to that tcp port.
#
# :5349 is NOT in this list any more. Under the passthrough shape LiveKit terminates its own
# TLS there, so it is an ordinary TURNS socket, not a plaintext one. INV-1, the ordering
# constraint it imposed on every other step, and the hard netfilter-persistent dependency for
# it are all gone — deleted, not mitigated. (Leaving :5349 publicly reachable is harmless: it
# is the same TURNS service :443 fronts. Operators who prefer the smaller surface may add it
# back, but nothing here DEPENDS on that.)
FW_PORTS=(8443)

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
# boot-correctness and rollback-restore bugs a diff-read cannot (INV-3 / INV-6). Exiting
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
for f in "$CADDY_MUX" "$HAPROXY_TMPL" "${HERE}/rollback.sh"; do
  [ -s "$f" ] || die "missing required artifact: $f"
done
[ -s "$LIVEKIT_YAML" ] || die "no livekit.yaml at $LIVEKIT_YAML"
# A prior (aborted) cutover left a .stock file → the current live file may be half-mutated.
# Refuse rather than clobber the true original: operator must rollback.sh first.
[ -e "$CADDY_STOCK" ] && die "$CADDY_STOCK already exists — a prior cutover is in progress/aborted. Run rollback.sh first, then retry."

# livekit.yaml is NOT in the rollback set: the passthrough shape never mutates it, so LiveKit is
# not one of the moving parts. What this cutover DOES require is that LiveKit is ALREADY
# terminating its own TLS on :5349 for TURN_DOMAIN — asserted below, before :443 moves, because
# peeling the turn SNI into a backend that cannot complete a handshake is strictly worse than
# not muxing at all. (The old shape asserted host networking here, because INV-1's :5349 DROP
# had to sit in the packets' real path. No plaintext, no INV-1, no assertion needed.)
command -v netfilter-persistent >/dev/null 2>&1 || die "netfilter-persistent absent — the :8443 DROP would not survive a reboot (INV-8: Caddy's HTTPS left publicly reachable). Install iptables-persistent first."
# openssl is load-bearing for BOTH turn-TLS verifies (the backend check before the move, and the
# check through :443 after). A missing openssl would make them pass vacuously — fail-OPEN.
command -v openssl >/dev/null 2>&1 || die "openssl absent — the turn-TLS verifies would fail OPEN without it"
command -v ss >/dev/null 2>&1 || die "ss absent — needed for the listener/owner checks"

# ---- WRONG-ENTRYPOINT GUARD — must precede EVERY mutation below (Carnot, HIGH) ----
# This script is for a box where CADDY owns :443. Run it on an already-muxed box (enspyr) and
# the old ordering was catastrophic: `systemctl disable --now haproxy` ran FIRST, taking the
# LIVE :443 mux down, and only THEN did the backend assertion fail — with `die`, not roll(),
# because Phase 0 is "before any mutation". Except it wasn't: the mutation was the first thing
# that happened. The script detected the wrong universe only after damaging it.
#
# So the entrypoint is asserted from the OUTSIDE, by who owns :443, before anything is touched.
_p443_owner() { ss -tlnpH 'sport = :443' 2>/dev/null | grep -oE '"[^"]+"' | head -1 | tr -d '"'; }
P443_OWNER="$(_p443_owner)"
case "$P443_OWNER" in
  haproxy)
    die "HAProxy already owns :443 — this box is already muxed. cutover.sh would take the LIVE front door down before it could tell. Use migrate-to-passthrough.sh instead." ;;
  caddy|"")
    log "  :443 owner is '${P443_OWNER:-<unbound>}' — correct entrypoint for cutover.sh" ;;
  *)
    die "an unexpected process ('$P443_OWNER') owns :443 — refusing to cut over a topology this script does not model." ;;
esac

# THE BACKEND MUST BE ALIVE BEFORE WE MOVE THE FRONT DOOR — and, per the above, before we touch
# ANYTHING. Under passthrough HAProxy holds no cert and cannot answer a turn handshake itself,
# so LiveKit must already be serving TLS for TURN_DOMAIN on :5349. Checking the SUBJECT (not
# merely "a handshake happened") is what makes this a real check: a dead socket and a wrong-cert
# socket fail differently, and both must fail here.
# SCOPE, stated precisely (Carnot): this proves a TLS handshake completes and the cert is valid
# for TURN_DOMAIN. It does NOT prove the TURN protocol stack behind it works — a listener with
# the right cert and a broken TURN implementation passes this. That is B3's job, off-box, as the
# acceptance gate. Naming the boundary here so the phase is not read as more than it measures.
log "asserting LiveKit already serves a valid $TURN_DOMAIN cert on :5349 (TLS identity; TURN protocol viability is B3's gate)"
_turn_backend_ok() {
  local out
  out="$(echo | timeout 10 openssl s_client -connect "127.0.0.1:5349" -servername "$TURN_DOMAIN" 2>/dev/null)" || return 1
  echo "$out" | openssl x509 -noout -checkhost "$TURN_DOMAIN" >/dev/null 2>&1
}
_turn_backend_ok || die "LiveKit is not serving a valid $TURN_DOMAIN cert on :5349. Fix livekit.yaml (cert_file/key_file + the /certs mount) BEFORE cutting :443 over — a mux in front of a dead backend is worse than no mux."

# ---- only now may we mutate ----
command -v haproxy >/dev/null 2>&1 || { log "installing haproxy"; apt-get install -y haproxy >/dev/null || die "haproxy install failed"; }
id haproxy >/dev/null 2>&1 || die "haproxy user missing after install"
# DISABLE, not just stop (Tesla P0): apt's postinst ENABLES+starts haproxy. With the mux config
# about to be installed to /etc/haproxy/haproxy.cfg while Caddy still owns public :443, an
# enabled haproxy would START on any reboot in the Phase-0→2.4 window and double-bind :443. Keep
# it disabled until 2.4 flips it on intentionally, so no reboot mid-cutover can race the acceptor.
# Safe here ONLY because the entrypoint guard above proved HAProxy does not own :443.
systemctl disable --now haproxy 2>/dev/null || true

# Render the mux config for this box's turn domain and config-check it. Rendering from a single
# template is deliberate: two hand-maintained per-box configs is exactly the repo↔runtime drift
# that task #10 was filed for.
# mktemp, not a predictable /tmp name: this runs as root, and `sed > /tmp/<fixed-name>` in a
# world-writable dir follows a pre-planted symlink with root privileges.
log "rendering haproxy.cfg for $TURN_DOMAIN"
RENDERED="$(mktemp /tmp/haproxy.cfg.rendered.XXXXXX)"
sed "s/@@TURN_DOMAIN@@/${TURN_DOMAIN}/g" "$HAPROXY_TMPL" > "$RENDERED"
grep -q '@@' "$RENDERED" && { rm -f "$RENDERED"; die "unrendered placeholder left in the rendered haproxy.cfg"; }
grep -qi 'ssl crt' "$RENDERED" && { rm -f "$RENDERED"; die "rendered config terminates TLS — that is the OLD external_tls template, not passthrough"; }
install -D -m 0644 "$RENDERED" /etc/haproxy/haproxy.cfg
rm -f "$RENDERED"
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
log "staging the .stock backup (the rollback set — ONE artifact now, not four)"
cp "$CADDYFILE"    "$CADDY_STOCK"    || die "failed to back up $CADDYFILE"
cmp -s "$CADDYFILE"    "$CADDY_STOCK"    || die "Caddyfile backup verification (cmp) failed"
FW_SNAP="$(mktemp /tmp/cutover-iptables.XXXXXX)"; iptables-save > "$FW_SNAP" 2>/dev/null || true
log "firewall snapshot at $FW_SNAP"

# ============================ PHASE 2 — the sequenced cutover =================================
# --- 2.1 INV-8: firewall :8443 (both families, verified) before Caddy moves there -------------
# Once this mutates iptables, a bare `die` would strand a partial DROP, so every failure from
# here uses roll(). Note what this is NOT any more: there is no ordering CONSTRAINT left. The
# old 2.1 had to precede 2.2 because 2.2 created public plaintext; passthrough creates none, so
# this step is merely "close Caddy's new port", not a race against a security window.
log "2.1 firewalling :8443 (v4+v6, loopback-only) before Caddy moves onto it"
fw_block_ports || roll "could not confirm the loopback DROP on both families — unwinding (rollback removes any partial rule)"
# Persist fail-closed: a runtime-only rule is cleared by a reboot (Carnot P0).
netfilter-persistent save >/dev/null 2>&1 || roll "netfilter-persistent save FAILED — the DROP would not survive a reboot; unwinding"
ckpt CP1

# --- 2.2 DELETED (task #8) -------------------------------------------------------------------
# This step used to flip livekit.yaml to external_tls (plaintext on 5349) with a turn-scoped awk
# edit, restart LiveKit, and then run a four-part liveness gate to distinguish "genuinely
# plaintext" from "dead socket" — because a dead socket also fails a TLS handshake, which was
# the original false-green. Under passthrough LiveKit keeps its own TLS, so there is no edit, no
# restart, no fourth backup artifact, and no false-green to guard against. The one thing that
# remains is the POSITIVE assertion, and it now runs in Phase 0 where aborting is free.
#
# Deliberately left as a comment rather than deleted silently: three cage-match rounds argued
# about the code that was here, and someone reading the rehearsal RESULTS will look for it.
ss -tlnpH 'sport = :5349' 2>/dev/null | grep -q 'livekit-server' \
  || roll "the process listening on :5349 is not livekit-server — refusing to point the mux at it"
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
port_listening 443  || roll ":443 not bound after haproxy start"
ckpt CP4

# ============================ PHASE 3 — on-box verify (auto-rollback on any red) ==============
log "3 verifying (chat / livekit route + turn TLS identity and PATH on :443; TURN protocol = B3)"
sleep 2
# No -f: we verify the MUX ROUTES the SNI to Caddy and gets a response, not the HTTP status
# (chat's app / livekit's root may legitimately be non-2xx). A routing/passthrough failure is
# a curl transport error (exit != 0); an HTTP 404/401 is a successful route.
# --resolve pins the request to THIS box's loopback (Carnot). Previously these were bare
# `curl https://chat.enspyr.co/` with the domains HARDCODED while only TURN_DOMAIN was
# templated — so running the cutover on imagineering would have verified the mux by fetching
# ENSPYR, over the internet, and very likely PASSED. Not a check that fails on the wrong box:
# a check that succeeds by measuring a different machine. The domains are now derived from
# TURN_DOMAIN and the request is forced onto the local :443 the cutover just bound.
CHAT_DOMAIN="${CHAT_DOMAIN:-chat.${TURN_DOMAIN#turn.}}"
LK_DOMAIN="${LK_DOMAIN:-livekit.${TURN_DOMAIN#turn.}}"
curl -sS -o /dev/null --max-time 10 --resolve "${CHAT_DOMAIN}:443:127.0.0.1" "https://${CHAT_DOMAIN}/" 2>/dev/null \
  || roll "${CHAT_DOMAIN} not routing through the mux on THIS box"
curl -sS -o /dev/null --max-time 10 --resolve "${LK_DOMAIN}:443:127.0.0.1" "https://${LK_DOMAIN}/" 2>/dev/null \
  || roll "${LK_DOMAIN} not routing through the mux on THIS box"
# turn:443 must now complete TLS end-to-end with LiveKit through the passthrough. Full TURN
# allocation is the OFF-BOX b3 probe (acceptance gate below) — on-box we prove TLS terminates.
#
# Three corrections here, all Tesla, all the same class as the curl fix above:
#   1. 127.0.0.1, not "${TURN_DOMAIN}:443". Connecting by NAME leaves the box via DNS and can
#      certify a DIFFERENT machine's :443 while this acceptor is unbound or misrouted.
#   2. -checkhost, not `grep BEGIN CERTIFICATE`. "A cert appeared" is satisfied by ANY cert.
#   3. And the one that matters most: NEITHER of those, NOR a fingerprint comparison, can see
#      the failure this check exists for. `Caddyfile.mux` deliberately keeps a turn. site block,
#      and Caddy serves it from THE SAME cert store LiveKit mounts. So if a mis-rendered SNI
#      rule dumps turn traffic into default_backend be_caddy, Caddy answers with a byte-
#      IDENTICAL cert: checkhost passes, fingerprints match, roll() never fires. A verifier that
#      shares a representation with the thing it verifies is blind to bugs in that shared layer.
#
# So discriminate the PATH, not the certificate. Caddy's turn block answers `respond "turn" 200`
# to an HTTPS GET; LiveKit's TURN socket cannot speak HTTP at all. A successful GET through :443
# for the turn name therefore PROVES the SNI landed on Caddy — the exact misroute — and a failed
# one is what correct passthrough looks like.
echo | timeout 8 openssl s_client -connect 127.0.0.1:443 -servername "$TURN_DOMAIN" 2>/dev/null \
  | openssl x509 -noout -checkhost "$TURN_DOMAIN" >/dev/null 2>&1 \
  || roll "turns:443 on THIS box did not present a cert valid for $TURN_DOMAIN through the mux"
if curl -sS -o /dev/null --max-time 8 --resolve "${TURN_DOMAIN}:443:127.0.0.1" "https://${TURN_DOMAIN}/" 2>/dev/null; then
  roll "an HTTPS GET for $TURN_DOMAIN through :443 SUCCEEDED — the turn SNI is being answered by CADDY (default_backend), not passed through to LiveKit. Cert and fingerprint checks CANNOT see this: Caddy serves the same store LiveKit mounts."
fi
log "  turn SNI is NOT answered by Caddy (HTTP GET refused) — the passthrough path is real"

# ============================ PHASE 4 — renewal must be guarded ==============================
# The cert time-bomb did not disappear with cert-sync, it MOVED. LiveKit holds the cert and has
# no hot-reload, so a renewal without a restart serves a stale cert and TURN-over-TLS dies
# silently ~89d from now. cert-restart.{sh,service,timer} is that guard — two-clock gated, so it
# restarts only on a genuine renewal and cannot thrash. Same reasoning as the cert-sync
# assertion it replaces: a named landmine gets a hard gate, not a soft WARN.
#
# But the timer is NOT universally installable, and getting that wrong would have been a bad
# bug: cert-restart.service is BOOTSTRAP-only by contract — "install on island-dedicated boxes
# (enspyr), NEVER on a shared multi-tenant box (imagineering uses the runbook)". A machine-forced
# `docker restart livekit` on a box also running matrix/outline/bots is exactly what that
# forbids. So the gate is not "the timer is active"; it is "SOMEONE is named as the owner of
# renewal". Fail-closed with a declared opt-out beats fail-closed with no way through — the
# latter just gets commented out by the first operator who hits it.
log "4 renewal ownership (LiveKit owns the cert now; it cannot hot-reload one)"
case "${CERT_RENEWAL_OWNER:-timer}" in
  timer)
    # is-ENABLED as well as is-active (Tesla): `systemctl start` without `enable` greens an
    # is-active check and then evaporates on the next reboot, taking the sole owner of INV-4'
    # with it. Measure the persistence, not the transient.
    systemctl is-enabled --quiet cert-restart.timer 2>/dev/null \
      || roll "cert-restart.timer is not ENABLED — it would not survive a reboot, and it is the only thing standing between a renewal and a dead turns:443 ~89d out. \`systemctl enable --now cert-restart.timer\`, then re-run."
    systemctl is-active --quiet cert-restart.timer \
      || roll "cert-restart.timer is not active — LiveKit would serve a stale cert at the next renewal and TURN-over-TLS would fail silently. Wire it (deploy/media/cert-restart.*, task #3), or declare CERT_RENEWAL_OWNER=runbook if this is a shared box where the timer must not be installed."
    log "  cert-restart.timer is active — renewal restarts are automatic" ;;
  runbook)
    # CLIENT+REPAIR boxes: a human owns the restart. Require the DETECTOR at least, so the
    # renewal is not silently unobserved — an unowned renewal and an unwatched one fail the
    # same way, months later, with every dashboard green.
    log "  CERT_RENEWAL_OWNER=runbook — no timer here (shared box)."
    systemctl is-active --quiet served-cert-alarm.timer \
      || log "  WARN: served-cert-alarm.timer is not active either. Nothing on this box will NOTICE a stale served cert. RUNBOOK.md 'renewal' is now a manual obligation with a ~89d fuse."
    ;;
  *) roll "CERT_RENEWAL_OWNER='${CERT_RENEWAL_OWNER}' is not a recognised value (timer|runbook) — failing closed rather than guessing who owns cert renewal." ;;
esac

log "================================================================"
log "CUTOVER on-box verify GREEN. :443 dark window ${DARK_END:+$((DARK_END - DARK_START))ms}."
log "FINAL ACCEPTANCE (run from OFF-BOX now): b3_relay_probe against turns:443 must flip"
log "  UNREACHABLE → ALLOCATED, with RFC1918/CGNAT still 403. If it FAILS → run rollback.sh."
log "Rollback anytime: sudo bash ${HERE}/rollback.sh"
log "================================================================"
