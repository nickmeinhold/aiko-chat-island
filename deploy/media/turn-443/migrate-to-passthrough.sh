#!/usr/bin/env bash
# migrate-to-passthrough.sh — move a box that ALREADY runs the HAProxy :443 mux from the
# external_tls (terminate-and-forward-plaintext) shape to plain SNI passthrough.
#
# This is NOT cutover.sh. HAProxy keeps :443 the whole time; only its turn BACKEND changes, via
# a graceful reload. There is no dark window on :443 and Caddy is never touched. The one
# service interruption is a LiveKit restart (media bounce, seconds) when it stops taking
# plaintext and starts terminating its own TLS.
#
# What this migration DELETES, permanently:
#   haproxy-cert-sync.{sh,service,timer} · the concatenated PEM and its uid boundary ·
#   the .needs-reload sentinel (the round-4 P0) · plaintext TURN on a public-facing socket ·
#   INV-1 and the ordering constraint it imposed on every other step.
# What it makes LOAD-BEARING: cert-restart.timer. LiveKit has no cert hot-reload, so once it
# owns the cert a renewal without a restart serves a stale cert and TURN dies. Wiring that timer
# is part of THIS change, not a follow-up — shipping the swap without it plants a time bomb
# dated to the next renewal. Phase 4 refuses to finish if it is not active.
#
# Fail-closed: every precondition is checked BEFORE any mutation; any verify failure inside a
# phase restores what that phase touched and exits non-zero.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TURN_DOMAIN="${TURN_DOMAIN:?set TURN_DOMAIN (e.g. turn.enspyr.co)}"
# Shared assertions — ONE door (see lib/turn-assert.sh). cutover.sh sources the same file, so a
# correction lands in both paths or in neither. Round 3 caught the fix landing in 3 of 4 sites
# and missing THIS script — the one that runs on the live already-muxed box.
# shellcheck source=lib/turn-assert.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/turn-assert.sh"
turn_domain_valid "$TURN_DOMAIN" || { echo "[passthrough] ABORT: TURN_DOMAIN '$TURN_DOMAIN' is not a plain DNS name — refusing to render root-owned config from it." >&2; exit 1; }
# No $HOME default: this runs under sudo, where $HOME is /root, and "~/apps/livekit" would
# silently resolve to a path that does not exist — or worse, on a box where it does. Both this
# and TURN_DOMAIN are required and unguessable on purpose.
LIVEKIT_DIR="${LIVEKIT_DIR:?set LIVEKIT_DIR (the dir holding livekit.yaml + docker-compose.yml)}"
TURN_TLS_PORT="${TURN_TLS_PORT:-5349}"
LK_YAML="$LIVEKIT_DIR/livekit.yaml"
HA_CFG=/etc/haproxy/haproxy.cfg
STOCK_SUFFIX=".pre-passthrough"

log()  { echo "[passthrough] $*"; }
die()  { echo "[passthrough] ABORT (nothing mutated past this point): $*" >&2; exit 1; }
# For aborts AFTER a phase has mutated and unwound: "nothing mutated" is false there and reads as
# a stronger safety claim than the run actually earned. Caught by the round-3 RED proof, where a
# correct restore printed the wrong reassurance.
die_restored() { echo "[passthrough] ABORT (changes were made and have been RESTORED — see the restore lines above): $*" >&2; exit 1; }
dc()   { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
listening() { [ -n "$(ss -tlnH "sport = :$1" 2>/dev/null)" ]; }

# Local wrapper over the shared assertion, so the many call sites below stay readable.
serves_tls_for_domain() { turn_tls_ok 127.0.0.1 "$TURN_TLS_PORT" "$TURN_DOMAIN"; }

# ============================ PHASE 0 — preconditions (read-only) ============================
log "PHASE 0 — preconditions"

[ "$(id -u)" -eq 0 ] || die "must run as root"
command -v openssl >/dev/null || die "openssl missing — it is load-bearing for the TLS verify below"
[ -f "$LK_YAML" ]  || die "no livekit.yaml at $LK_YAML"
[ -f "$HA_CFG" ]   || die "no haproxy.cfg at $HA_CFG"
[ -f "$HERE/haproxy.cfg.tmpl" ] || die "missing haproxy.cfg.tmpl next to this script"

systemctl is-active --quiet haproxy || die "haproxy is not active — this script migrates a box that ALREADY runs the mux; use cutover.sh instead"
listening 443 || die ":443 is not bound — refusing to migrate a half-configured box"

# ---- WHAT SHAPE IS THIS BOX IN? Ask before judging the staging files (Tesla, round 3) ----
# Ordering matters and got it wrong once. The stock-file guards used to run FIRST, so the window
# between "Phase 2's reload succeeded" and "Phase 3 consumed the stocks" — a fully working
# passthrough box that still has both .pre-passthrough files — hit the both-stocks `die` and got
# told to restore BOTH. That recipe reloads the OLD terminating haproxy.cfg against a LiveKit
# that now speaks TLS: precisely the out-of-phase pair restore_both exists to prevent. The
# staging files are evidence, not a verdict; read the RUNNING shape first and let it arbitrate.
#
# Shape predicates are scoped to the top-level `turn:` mapping, exactly like Phase 1's mutation
# (Carnot): a bare whole-file grep can certify the wrong shape from an unrelated section, and a
# stray external_tls anywhere could block a valid resume. The check reads the region the edit writes.
turn_key() {  # turn_key <key-regex> — true if the key exists inside the top-level turn: mapping
  awk -v pat="$1" '
    /^[^[:space:]]/ { in_turn = ($0 ~ /^turn:/) }
    in_turn && $0 ~ pat { found=1 }
    END { exit(found ? 0 : 1) }' "$LK_YAML"
}
is_passthrough_shape() {
  ! turn_key '^[[:space:]]+external_tls:[[:space:]]*true' \
    && turn_key '^[[:space:]]+cert_file:' \
    && ! grep -qi 'ssl crt' "$HA_CFG" \
    && grep -qF "req_ssl_sni -i ${TURN_DOMAIN}" "$HA_CFG" \
    && serves_tls_for_domain
}

ALREADY_PASSTHROUGH=0
if is_passthrough_shape; then
  ALREADY_PASSTHROUGH=1
  # Stale staging files from a run that got as far as a successful Phase 2 are now INVALID —
  # the old haproxy.cfg they hold cannot be paired with a TLS-speaking LiveKit. Discard them
  # rather than leaving a trap that tells the next operator to reassemble a broken pair.
  if [ -e "$LK_YAML$STOCK_SUFFIX" ] || [ -e "$HA_CFG$STOCK_SUFFIX" ]; then
    log "  discarding stale .pre-passthrough files: the box is already passthrough, so restoring them would recreate the out-of-phase pair"
    rm -f "$LK_YAML$STOCK_SUFFIX" "$HA_CFG$STOCK_SUFFIX"
  fi
  log "  box is ALREADY in the passthrough shape and serving TLS — resuming at the Phase 4 gate"
else
  # Not passthrough. NOW the staging files tell you where a previous run died, and WHICH ones
  # exist determines what "restore by hand" actually means.
  if [ -e "$LK_YAML$STOCK_SUFFIX" ] && [ ! -e "$HA_CFG$STOCK_SUFFIX" ]; then
    die "$LK_YAML$STOCK_SUFFIX exists but $HA_CFG$STOCK_SUFFIX does not — a previous run died
between Phase 1 and Phase 2. LiveKit is terminating its own TLS; HAProxy is still on the old
external_tls config forwarding PLAINTEXT to :$TURN_TLS_PORT. turns:443 is DOWN right now.

To unwind (HAProxy was never modified, so only LiveKit must move):
    mv -f '$LK_YAML$STOCK_SUFFIX' '$LK_YAML'
    (cd '$LIVEKIT_DIR' && docker compose restart livekit)
then verify turns:443 answers and re-run this script."
  fi
  for f in "$LK_YAML$STOCK_SUFFIX" "$HA_CFG$STOCK_SUFFIX"; do
    [ -e "$f" ] && die "$f exists — a previous run aborted mid-Phase-2, and this box is NOT in the
passthrough shape. Both artifacts were staged, so restore BOTH together (they are one coupled
artifact — a plaintext-forwarding proxy against a TLS-expecting backend is dead TURN with both
halves looking restored):
    mv -f '$HA_CFG$STOCK_SUFFIX' '$HA_CFG' && systemctl reload haproxy
    mv -f '$LK_YAML$STOCK_SUFFIX' '$LK_YAML' && (cd '$LIVEKIT_DIR' && docker compose restart livekit)
then verify turns:443 answers and re-run this script."
  done
  # Neither shape, no staging files: refuse to guess.
  turn_key '^[[:space:]]+external_tls:[[:space:]]*true' \
    || die "livekit.yaml has no external_tls:true, but the box is not in a verified passthrough shape either — refusing to guess. Inspect $LK_YAML and $HA_CFG by hand."
fi

# The cert LiveKit is about to serve must already be present, valid, and for the right name.
# Read it from the container's own view: a host-side path that looks fine but is mounted
# differently is precisely how this fails silently.
CERT_IN_CTR="/certs/${TURN_DOMAIN}.crt"
KEY_IN_CTR="/certs/${TURN_DOMAIN}.key"
docker exec livekit test -r "$CERT_IN_CTR" 2>/dev/null \
  || die "livekit container cannot read $CERT_IN_CTR — check the /certs mount in docker-compose.yml"
docker exec livekit test -r "$KEY_IN_CTR" 2>/dev/null \
  || die "livekit container cannot read $KEY_IN_CTR"
log "  cert + key readable inside the container"

# Same subject check as above, but against the file, before we commit to serving it.
docker exec livekit sh -c "cat $CERT_IN_CTR" 2>/dev/null \
  | openssl x509 -noout -checkhost "$TURN_DOMAIN" >/dev/null 2>&1 \
  || die "the mounted cert is not valid for $TURN_DOMAIN"
docker exec livekit sh -c "cat $CERT_IN_CTR" 2>/dev/null \
  | openssl x509 -noout -checkend 86400 >/dev/null 2>&1 \
  || die "the mounted cert expires within 24h — renew before migrating, not during"
log "  cert is valid for $TURN_DOMAIN and not expiring imminently"

log "PHASE 0 OK"

# Phases 1-3 are skipped entirely on the resume path (already-migrated box re-running to clear
# the Phase 4 gate). Phase 4 is NOT skipped — it is the whole reason a resume exists.
if [ "$ALREADY_PASSTHROUGH" -eq 0 ]; then

# ============================ PHASE 1 — LiveKit takes back its TLS ==========================
log "PHASE 1 — livekit.yaml: external_tls:true -> cert_file/key_file"
cp -a "$LK_YAML" "$LK_YAML$STOCK_SUFFIX"

restore_livekit() {
  log "  restoring livekit.yaml and restarting"
  mv -f "$LK_YAML$STOCK_SUFFIX" "$LK_YAML"
  (cd "$LIVEKIT_DIR" && dc restart livekit >/dev/null 2>&1) || true
}

# Scoped to the top-level turn: mapping (same discipline as the cutover's awk edit): replace the
# external_tls line in place with the two cert lines, touching nothing else.
awk -v crt="$CERT_IN_CTR" -v key="$KEY_IN_CTR" '
  /^turn:/            { inturn=1 }
  /^[a-zA-Z]/ && !/^turn:/ { inturn=0 }
  inturn && /^[[:space:]]*external_tls:[[:space:]]*true/ {
      print "  cert_file: " crt; print "  key_file: " key; next }
  { print }
' "$LK_YAML$STOCK_SUFFIX" > "$LK_YAML.new"

grep -qE '^\s*cert_file:' "$LK_YAML.new" && grep -qE '^\s*key_file:' "$LK_YAML.new" \
  || { rm -f "$LK_YAML.new"; restore_livekit; die "awk edit did not produce cert_file/key_file"; }
grep -qE '^\s*external_tls:' "$LK_YAML.new" \
  && { rm -f "$LK_YAML.new"; restore_livekit; die "external_tls survived the edit"; }
mv -f "$LK_YAML.new" "$LK_YAML"
log "  edited (diff vs original: $(diff <(cat "$LK_YAML$STOCK_SUFFIX") "$LK_YAML" | grep -c '^[<>]') lines)"

(cd "$LIVEKIT_DIR" && dc restart livekit >/dev/null) || { restore_livekit; die_restored "livekit restart failed"; }

# Same scope note as cutover.sh: TLS identity, not TURN protocol viability. The acceptance
# gates named at the end (B3 + the real-client relay proof) are what prove the protocol.
log "  waiting for LiveKit to serve a valid $TURN_DOMAIN cert on :$TURN_TLS_PORT (TLS identity)"
ok=0
for _ in $(seq 1 60); do
  if listening "$TURN_TLS_PORT" && serves_tls_for_domain; then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || { restore_livekit; die_restored "LiveKit did not present a valid $TURN_DOMAIN cert on :$TURN_TLS_PORT"; }
log "PHASE 1 OK — LiveKit is terminating its own TLS"

# ============================ PHASE 2 — HAProxy backend swap ================================
log "PHASE 2 — render + install the passthrough haproxy.cfg (graceful reload, no dark window)"
cp -a "$HA_CFG" "$HA_CFG$STOCK_SUFFIX"

sed "s/@@TURN_DOMAIN@@/${TURN_DOMAIN}/g" "$HERE/haproxy.cfg.tmpl" > "$HA_CFG.new"
# These two guards fire BEFORE haproxy.cfg is replaced, but NOT before Phase 1 mutated LiveKit —
# so they must unwind LiveKit, and must not use die()'s "nothing mutated" wording. (The stock
# haproxy.cfg is still in place here, which is why this is restore_livekit and not restore_both.)
if grep -q '@@' "$HA_CFG.new"; then
  rm -f "$HA_CFG.new"; rm -f "$HA_CFG$STOCK_SUFFIX"; restore_livekit
  die_restored "unrendered placeholder left in the rendered haproxy.cfg (LiveKit restored)"
fi
if grep -qi 'ssl crt' "$HA_CFG.new"; then
  rm -f "$HA_CFG.new"; rm -f "$HA_CFG$STOCK_SUFFIX"; restore_livekit
  die_restored "rendered config still terminates TLS — wrong template (LiveKit restored)"
fi
mv -f "$HA_CFG.new" "$HA_CFG"

# Restoring HAProxy ALONE would be worse than doing nothing. By this point LiveKit has already
# been switched to terminate its own TLS, so putting back the external_tls haproxy.cfg — which
# forwards PLAINTEXT to :5349 — pairs a plaintext-speaking proxy with a TLS-expecting backend
# and TURN is dead, with both halves individually looking "restored". The two are one coupled
# artifact for rollback purposes even though they are two files.
restore_both() {
  log "  restoring the previous haproxy.cfg and reloading"
  mv -f "$HA_CFG$STOCK_SUFFIX" "$HA_CFG"
  systemctl reload haproxy || systemctl restart haproxy || true
  restore_livekit
  # Verify the pair is actually back in AGREEMENT — and note what "agreement" means here.
  # An earlier version verified with `openssl s_client :443 -checkhost`, which is blind by
  # construction (Tesla): under the OLD shape HAProxy TERMINATES :443, so that handshake
  # completes against HAProxy's own cert whether LiveKit came back, stayed on TLS, or died —
  # and restore_livekit ends in `|| true`. Measuring the front oscillator says nothing about
  # whether the pair is in phase.
  #
  # The restored shape is: HAProxy terminates :443 AND forwards PLAINTEXT to :5349. So the
  # backend-side assertion is the inverse of the passthrough one — :5349 must be alive and must
  # NOT present TLS — and the front must answer. Both, or this is not a verified restore.
  local ok_front=0 ok_back=0
  for _ in $(seq 1 30); do
    if echo | timeout 6 openssl s_client -connect "127.0.0.1:443" -servername "$TURN_DOMAIN" 2>/dev/null \
         | openssl x509 -noout -checkhost "$TURN_DOMAIN" >/dev/null 2>&1; then ok_front=1; break; fi
    sleep 1
  done
  if listening "$TURN_TLS_PORT" && ! serves_tls_for_domain; then ok_back=1; fi
  if [ "$ok_front" = 1 ] && [ "$ok_back" = 1 ]; then
    log "  restore verified: :443 answers AND :$TURN_TLS_PORT is back to plaintext (the external_tls pair)"
    return 0
  fi

  echo "" >&2
  echo "[passthrough] ############## ROLLBACK UNVERIFIED — TURN IS DOWN ##############" >&2
  echo "  Both artifacts were restored to the PRE-migration shape ON DISK, but the running pair" >&2
  echo "  does not agree:  :443 answers=$ok_front   :$TURN_TLS_PORT back-to-plaintext=$ok_back" >&2
  echo "  (:443 answering ALONE proves nothing — HAProxy terminates it under the old shape.)" >&2
  echo "  Do NOT walk away. Check, in this order:" >&2
  echo "    docker logs livekit --tail 50  (did it come back on external_tls?)" >&2
  echo "    systemctl status haproxy       (is it running with the restored config?)" >&2
  echo "    diff $HA_CFG $HA_CFG$STOCK_SUFFIX" >&2
  echo "  Exiting 4 = 'migration failed AND recovery unproven'." >&2
  exit 4
}

haproxy -c -f "$HA_CFG" >/dev/null 2>&1 || { restore_both; die_restored "haproxy -c rejected the rendered config"; }
systemctl reload haproxy || { restore_both; die_restored "haproxy reload failed"; }

listening 443 || { restore_both; die_restored ":443 is not bound after the reload"; }

log "  verifying a real TLS handshake for $TURN_DOMAIN THROUGH :443"
ok=0
for _ in $(seq 1 15); do
  if echo | timeout 10 openssl s_client -connect "127.0.0.1:443" -servername "$TURN_DOMAIN" 2>/dev/null \
       | openssl x509 -noout -checkhost "$TURN_DOMAIN" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || { restore_both; die_restored "no valid $TURN_DOMAIN handshake through :443 after the swap"; }
# THE CHECK THIS SCRIPT WAS MISSING (Tesla, round 3). A handshake whose leaf -checkhosts for
# TURN_DOMAIN does NOT prove the stream reached LiveKit: Caddyfile.mux keeps a turn. block served
# from the same store LiveKit mounts, so a dropped/mis-rendered `use_backend be_turn` yields a
# byte-identical cert and this phase would log "passes turn SNI straight to LiveKit" over a mux
# that is Caddy wearing LiveKit's face. cutover.sh, checks.sh and faults.sh all grew this
# discriminator in round 2; THIS script — the one that runs on the live, already-muxed box with
# no dark window and no second chance — did not. Same door now.
turn_path_is_passthrough "$TURN_DOMAIN" \
  || { restore_both; die_restored "after the swap, an HTTPS GET for $TURN_DOMAIN through :443 SUCCEEDED — the turn SNI is being answered by CADDY, not passed through to LiveKit. The cert cannot show you this (same store)."; }
log "PHASE 2 OK — :443 passes turn SNI straight to LiveKit (path discriminated, not inferred from the cert)"

# ============================ PHASE 3 — decommission cert-sync ==============================
log "PHASE 3 — removing haproxy-cert-sync (it has nothing left to sync)"
# CONSUME THE STAGING FILES FIRST, before anything in this phase is destroyed (Tesla P1).
# Phase 3 shreds the PEM — the old terminator's private key. Once that is carbon, the stock
# haproxy.cfg is UNBOOTABLE (`bind ... ssl crt /etc/haproxy/certs/....pem` cannot be satisfied),
# so the "restore BOTH" recipe Phase 0 prints would reassemble a corpse: neither old nor new, a
# third state with unbootable turn and both files looking restored. Ordering the consumption
# before the shred means the moment the stocks stop being valid is the moment they stop existing.
rm -f "$LK_YAML$STOCK_SUFFIX" "$HA_CFG$STOCK_SUFFIX"
systemctl disable --now haproxy-cert-sync.timer >/dev/null 2>&1 || true
systemctl disable --now haproxy-cert-sync.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/haproxy-cert-sync.timer /etc/systemd/system/haproxy-cert-sync.service
# BOTH paths: the old cutover installed to /usr/local/bin (verified on enspyr, where the
# script is still on disk); /usr/local/sbin was where this line originally looked, and alone
# it silently left the script behind — the exact confusing archaeology this phase exists to
# prevent. rollback.sh already removed both; the two scripts now agree.
rm -f /usr/local/bin/haproxy-cert-sync.sh /usr/local/sbin/haproxy-cert-sync.sh
# The PEM held a copy of the private key outside caddy's uid. Shred it rather than unlink.
if [ -f "/etc/haproxy/certs/${TURN_DOMAIN}.pem" ]; then
  shred -u "/etc/haproxy/certs/${TURN_DOMAIN}.pem" 2>/dev/null \
    || rm -f "/etc/haproxy/certs/${TURN_DOMAIN}.pem"
  log "  shredded the concatenated PEM (it carried the private key across a uid boundary)"
fi
rmdir /etc/haproxy/certs 2>/dev/null || true
systemctl daemon-reload
log "PHASE 3 OK"

fi  # end: if ALREADY_PASSTHROUGH == 0

# ============================ PHASE 4 — cert-restart must be live ===========================
# Passthrough makes LiveKit the cert holder, and LiveKit cannot hot-reload one. Without this
# timer the next renewal serves a stale cert and TURN-over-TLS dies silently, months from now.
#
# Unlike cutover.sh this has no CERT_RENEWAL_OWNER=runbook escape, deliberately: this script
# only runs on a box that ALREADY runs the mux, which today means an island-dedicated BOOTSTRAP
# box where the timer is exactly what the contract prescribes. If that ever stops being true,
# add the fork here too rather than weakening the gate.
log "PHASE 4 — asserting cert-restart.timer is active"
# is-ENABLED as well as is-active (Tesla): `systemctl start` without `enable` greens an
# is-active check and evaporates on the next reboot, taking the sole owner of INV-4' with it.
if ! systemctl is-enabled --quiet cert-restart.timer 2>/dev/null || ! systemctl is-active --quiet cert-restart.timer; then
  echo "[passthrough] MIGRATION IS INCOMPLETE." >&2
  echo "  The data plane is live and correct, but cert-restart.timer is not BOTH enabled and active." >&2
  echo "  LiveKit now owns the cert and cannot hot-reload it, so the next renewal will serve" >&2
  echo "  a stale cert and TURN-over-TLS will fail silently. Wire it (task #3) and re-run." >&2
  echo "  Re-running is SAFE: Phase 0 detects the already-migrated shape, verifies it, and" >&2
  echo "  resumes at this gate without touching the live data plane." >&2
  exit 3
fi
log "PHASE 4 OK — renewal restarts are guarded"

log "MIGRATION COMPLETE. Now run the acceptance gates:"
log "  b3_relay_probe.py with B3_REQUIRE_ENDPOINT=tls:*:443   (relay-deny + liveness)"
log "  webrtc_relay_proof.py from a UDP-blocked vantage        (a real call actually flows)"
