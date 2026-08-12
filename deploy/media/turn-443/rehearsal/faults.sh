#!/usr/bin/env bash
# faults.sh — deliberate fault injection. Runs IN the rig, from the cut-over state.
#
# The happy path passing tells you almost nothing about a deploy script; what matters is
# whether each failure mode lands somewhere safe. Each fault below maps to an invariant in
# INVARIANTS.md and to a specific cage-match finding that was previously closed by argument.
set -uo pipefail
R=/opt/turn-443
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
PEM="/etc/haproxy/certs/${TURN_DOMAIN}.pem"
STORE="$(readlink -f /opt/turncerts)"
P=0; F=0
chk() { if [ "$2" -eq 0 ]; then echo "  PASS  $1"; P=$((P+1)); else echo "  FAIL  $1"; F=$((F+1)); fi; }
served_fp() { timeout 8 openssl s_client -connect "${TURN_DOMAIN}:443" -servername "$TURN_DOMAIN" </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }
store_fp()  { openssl x509 -in "${STORE}/${TURN_DOMAIN}.crt" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }

echo "=== FAULT 1 — cert RENEWAL propagates to what HAProxy actually serves (INV-4 positive) ==="
# The whole point of haproxy-cert-sync: Caddy renews, but HAProxy serves a SEPARATE PEM. If the
# sync is broken, TURNS silently dies ~89d out with every dashboard green.
before_served="$(served_fp)"; before_store="$(store_fp)"
chk "pre-state: what :443 serves == what Caddy holds" "$([ -n "$before_served" ] && [ "$before_served" = "$before_store" ] && echo 0 || echo 1)"
# Force a genuine re-issue: drop Caddy's stored cert and restart it (real ACME round-trip).
rm -f "${STORE}/${TURN_DOMAIN}".{crt,key,json}
systemctl restart caddy
for _ in $(seq 1 60); do [ -s "${STORE}/${TURN_DOMAIN}.crt" ] && break; sleep 2; done
after_store="$(store_fp)"
chk "Caddy re-issued a DIFFERENT cert" "$([ -n "$after_store" ] && [ "$after_store" != "$before_store" ] && echo 0 || echo 1)"
mid_served="$(served_fp)"
chk "HAProxy still serves the OLD cert until sync runs (expected)" "$([ "$mid_served" = "$before_served" ] && echo 0 || echo 1)"
TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_DIR=/opt/turncerts bash "$R/haproxy-cert-sync.sh" 2>&1 | sed 's/^/    /'
final_served="$(served_fp)"
chk "after cert-sync, :443 serves the NEW cert" "$([ "$final_served" = "$after_store" ] && echo 0 || echo 1)"

echo
echo "=== FAULT 2 — cert-sync when the RELOAD FAILS (INV-4, the round-4 P0) ==="
# The r4 bug: on reload failure the script rm'd the PEM, which makes `bind ... ssl crt <PEM>`
# unsatisfiable — haproxy then cannot START AT ALL. Verified empirically earlier: a missing PEM
# makes `haproxy -c` exit with "Fatal errors". So the fix must keep the PEM and defer the reload.
cp /etc/haproxy/haproxy.cfg /tmp/haproxy.cfg.good
echo "this-is-not-valid-haproxy-config" >> /etc/haproxy/haproxy.cfg   # make reload fail
rm -f "${STORE}/${TURN_DOMAIN}".{crt,key,json}; systemctl restart caddy
for _ in $(seq 1 60); do [ -s "${STORE}/${TURN_DOMAIN}.crt" ] && break; sleep 2; done
TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_DIR=/opt/turncerts bash "$R/haproxy-cert-sync.sh" 2>&1 | sed 's/^/    /'
rc=$?
chk "cert-sync reports failure (non-zero) when the reload fails" "$([ $rc -ne 0 ] && echo 0 || echo 1)"
chk "the PEM still EXISTS (never rm'd on reload failure)" "$([ -s "$PEM" ] && echo 0 || echo 1)"
openssl x509 -in "$PEM" -noout >/dev/null 2>&1; chk "the PEM still holds a valid cert" $?
openssl pkey -in "$PEM" -noout >/dev/null 2>&1; chk "the PEM still holds a valid key" $?
[ -e "${PEM}.needs-reload" ]; chk "a needs-reload sentinel was dropped for the next tick" $?
chk "HAProxy is still RUNNING (degraded, not down)" "$(systemctl is-active --quiet haproxy && echo 0 || echo 1)"
# Restore a good config and prove the deferred reload is actually retried + the sentinel cleared.
cp /tmp/haproxy.cfg.good /etc/haproxy/haproxy.cfg
haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null 2>&1; chk "with the PEM intact, haproxy.cfg is BOOTABLE again" $?
TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_DIR=/opt/turncerts bash "$R/haproxy-cert-sync.sh" 2>&1 | sed 's/^/    /'
chk "the owed reload was retried and the sentinel cleared" "$([ ! -e "${PEM}.needs-reload" ] && echo 0 || echo 1)"
chk "and :443 now serves the current cert" "$([ "$(served_fp)" = "$(store_fp)" ] && echo 0 || echo 1)"

echo
echo "=== FAULT 3 — SNI routing matrix (INV-7) ==="
timeout 8 openssl s_client -connect 127.0.0.1:443 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | grep -q "BEGIN CERT"
chk "turn SNI  -> terminator presents a cert" $?
timeout 8 openssl s_client -connect 127.0.0.1:443 -servername "chat.enspyr.co" </dev/null 2>/dev/null | grep -q "BEGIN CERT"
chk "chat SNI  -> passthrough to Caddy presents a cert" $?
# DIFFERENTIAL, not pattern-matching: the correct claim is "the mux is transparent", so compare
# the mux's answer for an unknown SNI against Caddy's OWN answer on :8443. (Matching an error
# string here first produced a false FAIL — the TLS alert goes to stderr, not stdout.)
mux_unknown="$(timeout 8 openssl s_client -connect 127.0.0.1:443  -servername nope.example.com </dev/null 2>&1 | grep -oE 'alert [a-z ]+|no peer certificate available' | sort -u | tr '\n' ';')"
dir_unknown="$(timeout 8 openssl s_client -connect 127.0.0.1:8443 -servername nope.example.com </dev/null 2>&1 | grep -oE 'alert [a-z ]+|no peer certificate available' | sort -u | tr '\n' ';')"
chk "unknown SNI through the mux == Caddy's own answer ('${mux_unknown}')" "$([ -n "$mux_unknown" ] && [ "$mux_unknown" = "$dir_unknown" ] && echo 0 || echo 1)"
timeout 8 openssl s_client -connect 127.0.0.1:443 -servername chat.enspyr.co </dev/null 2>/dev/null | grep -q "BEGIN CERT"
chk "a rejected handshake does not poison the acceptor (known SNI still works after)" $?
# Non-TLS junk must be REJECTED by the fe443 content-reject, not parked on a slot.
out="$(printf 'GET / HTTP/1.0\r\n\r\n' | timeout 8 nc 127.0.0.1 443 2>&1; echo "rc=$?")"
chk "non-TLS junk on :443 is dropped (no HTTP response served)" "$(echo "$out" | grep -q "HTTP/1" && echo 1 || echo 0)"

echo
echo "RESULT faults: pass=$P fail=$F"
[ $F -eq 0 ]
