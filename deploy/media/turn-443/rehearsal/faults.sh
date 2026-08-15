#!/usr/bin/env bash
# faults.sh — deliberate fault injection. Runs IN the rig, from the cut-over state.
#
# The happy path passing tells you almost nothing about a deploy script; what matters is
# whether each failure mode lands somewhere safe. Each fault below maps to an invariant in
# INVARIANTS.md and to a specific cage-match finding that was previously closed by argument.
set -uo pipefail
R=/opt/turn-443
TURN_DOMAIN="${TURN_DOMAIN:-turn.enspyr.co}"
STORE="$(readlink -f /opt/turncerts)"
P=0; F=0
chk() { if [ "$2" -eq 0 ]; then echo "  PASS  $1"; P=$((P+1)); else echo "  FAIL  $1"; F=$((F+1)); fi; }
served_fp() { timeout 8 openssl s_client -connect "${TURN_DOMAIN}:443" -servername "$TURN_DOMAIN" </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }
store_fp()  { openssl x509 -in "${STORE}/${TURN_DOMAIN}.crt" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }

echo "=== FAULT 1 — cert RENEWAL reaches what CLIENTS see (INV-4', the one that got HARDER) ==="
# Under passthrough LiveKit holds the cert, and pion/turn serves it from MEMORY. So a renewed
# file on disk coexists with a stale served cert until LiveKit restarts. That is strictly worse
# than the external_tls shape, where HAProxy hot-reloaded — it is the price of the trade, and
# the ONLY thing standing between it and a silent ~89d TURN outage is cert-restart.sh. So test
# it end to end, including the negative half (it must NOT restart when there is no renewal).
CR="${CR:-/opt/media}"      # where cert-restart.sh + served-cert-alarm.sh + lib/ are synced
before_served="$(served_fp)"; before_store="$(store_fp)"
chk "pre-state: what :443 serves == what Caddy holds" "$([ -n "$before_served" ] && [ "$before_served" = "$before_store" ] && echo 0 || echo 1)"

# NEGATIVE HALF FIRST (no-thrash): with disk == served, a run must decide NOT to restart.
lk_started_before="$(docker inspect livekit --format '{{.State.StartedAt}}' 2>/dev/null)"
(cd "$CR" && TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_LEAF_DIR=/opt/turncerts ALARM_NOTIFY_URL= \
   bash ./cert-restart.sh 2>&1 | sed 's/^/    /') || true
lk_started_after="$(docker inspect livekit --format '{{.State.StartedAt}}' 2>/dev/null)"
chk "no renewal pending -> LiveKit was NOT restarted (no thrash)" "$([ "$lk_started_before" = "$lk_started_after" ] && echo 0 || echo 1)"

# Force a genuine re-issue: drop Caddy's stored cert and restart it (real ACME round-trip).
rm -f "${STORE}/${TURN_DOMAIN}".{crt,key,json}
systemctl restart caddy
for _ in $(seq 1 60); do [ -s "${STORE}/${TURN_DOMAIN}.crt" ] && break; sleep 2; done
after_store="$(store_fp)"
chk "Caddy re-issued a DIFFERENT cert" "$([ -n "$after_store" ] && [ "$after_store" != "$before_store" ] && echo 0 || echo 1)"
mid_served="$(served_fp)"
chk "clients STILL see the OLD cert (the hazard is real, not theoretical)" "$([ "$mid_served" = "$before_served" ] && echo 0 || echo 1)"

# A renewal does NOT propagate immediately, and that is deliberate. cert-restart is a STALENESS
# guard, not a renewal detector: it fires only once the SERVED cert falls inside
# ALARM_NOTAFTER_DAYS. Caddy renews at ~30d remaining, so under passthrough a renewal reaches
# clients with a bounded lag of roughly (30d - threshold). The served cert stays valid the whole
# time, so there is no user impact — but it IS a real behavioural difference from cert-sync,
# which propagated on fingerprint change. Assert both halves of the actual contract.
(cd "$CR" && TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_LEAF_DIR=/opt/turncerts ALARM_NOTIFY_URL= \
   bash ./cert-restart.sh 2>&1 | sed 's/^/    /') || true
chk "a renewal alone does NOT restart while the served cert is still fresh (anti-thrash)" \
  "$([ "$(served_fp)" = "$before_served" ] && echo 0 || echo 1)"

# Now drive the branch that actually protects us: served cert INSIDE the staleness threshold
# with a newer cert on disk. Raising the threshold is how you reach that state without waiting
# 76 days, and it exercises the identical decision path.
(cd "$CR" && TURN_DOMAIN="$TURN_DOMAIN" CADDY_CERT_LEAF_DIR=/opt/turncerts ALARM_NOTIFY_URL= \
   ALARM_NOTAFTER_DAYS=99999 bash ./cert-restart.sh 2>&1 | sed 's/^/    /') || true
final_served="$(served_fp)"
chk "once the served cert reads STALE, cert-restart propagates the NEW cert to :443" \
  "$([ -n "$final_served" ] && [ "$final_served" = "$after_store" ] && echo 0 || echo 1)"

echo
echo "=== FAULT 2 — cutover REFUSES when the passthrough backend cannot serve TLS ==="
# The passthrough shape's characteristic failure: HAProxy holds no cert, so if LiveKit is not
# serving a valid cert for TURN_DOMAIN, peeling the turn SNI produces an endpoint that accepts
# a connection and then dies mid-handshake — worse than no mux at all. cutover.sh asserts the
# backend in PHASE 0, before any mutation. Prove that assertion has teeth by breaking it.
bash "$R/rollback.sh" >/dev/null 2>&1 || true
mv "${STORE}/${TURN_DOMAIN}.crt" "${STORE}/${TURN_DOMAIN}.crt.hidden" 2>/dev/null
docker restart livekit >/dev/null 2>&1; sleep 6
caddy_owns_443_before="$(ss -tlnpH 'sport = :443' 2>/dev/null | grep -c caddy)"
OFF443_PROVEN=1 TURN_DOMAIN="$TURN_DOMAIN" bash "$R/cutover.sh" >/tmp/fault2.log 2>&1
rc=$?
chk "cutover ABORTED (non-zero) with a dead turn backend" "$([ $rc -ne 0 ] && echo 0 || echo 1)"
grep -q "not serving a valid" /tmp/fault2.log
chk "  ...and said WHY (backend cert assertion), not a generic failure" $?
caddy_owns_443_after="$(ss -tlnpH 'sport = :443' 2>/dev/null | grep -c caddy)"
chk "NOTHING was mutated — Caddy still owns :443" "$([ "$caddy_owns_443_before" = "$caddy_owns_443_after" ] && [ "$caddy_owns_443_after" != "0" ] && echo 0 || echo 1)"
chk "no .stock was staged (abort happened before Phase 1)" "$([ ! -e /etc/caddy/Caddyfile.stock ] && echo 0 || echo 1)"
# Restore and re-cut so the remaining faults run from the cut-over state.
mv "${STORE}/${TURN_DOMAIN}.crt.hidden" "${STORE}/${TURN_DOMAIN}.crt" 2>/dev/null
docker restart livekit >/dev/null 2>&1; sleep 6
OFF443_PROVEN=1 TURN_DOMAIN="$TURN_DOMAIN" bash "$R/cutover.sh" >/dev/null 2>&1
chk "re-cutover succeeds once the backend is healthy again" "$([ -n "$(ss -tlnpH 'sport = :443' 2>/dev/null | grep haproxy)" ] && echo 0 || echo 1)"

echo
echo "=== FAULT 3 — SNI routing matrix (INV-7) ==="
timeout 8 openssl s_client -connect 127.0.0.1:443 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | grep -q "BEGIN CERT"
chk "turn SNI  -> LiveKit (through passthrough) presents a cert" $?
# And it must be LIVEKIT's cert, end-to-end — not something HAProxy synthesised. Compare the
# fingerprint through :443 against the one LiveKit serves directly on :5349.
lk_direct="$(timeout 8 openssl s_client -connect 127.0.0.1:5349 -servername "$TURN_DOMAIN" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
chk "  ...and it is byte-identical to LiveKit's own cert on :5349" "$([ -n "$lk_direct" ] && [ "$(served_fp)" = "$lk_direct" ] && echo 0 || echo 1)"
# That fingerprint match does NOT prove passthrough (Tesla): Caddyfile.mux keeps a turn. site
# block served from THE SAME store LiveKit mounts, so a misroute into be_caddy produces an
# IDENTICAL cert. The claim "true passthrough" was unearned. Discriminate the PATH instead —
# Caddy answers an HTTPS GET, LiveKit's TURN socket cannot.
if curl -sS -o /dev/null --max-time 8 --resolve "${TURN_DOMAIN}:443:127.0.0.1" "https://${TURN_DOMAIN}/" 2>/dev/null; then
  chk "  ...and the turn SNI is NOT answered by Caddy (TRUE passthrough)" 1
else
  chk "  ...and the turn SNI is NOT answered by Caddy (TRUE passthrough)" 0
fi
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
