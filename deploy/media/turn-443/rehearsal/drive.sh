#!/usr/bin/env bash
# drive.sh — host-side driver for the TURN-on-443 rehearsal (task #6).
#
# Runs on the Mac and drives the disposable Lima VM. Host-side because the reboot tests
# (INV-3) require an orchestrator that survives the guest rebooting.
#
# Usage:
#   ./drive.sh sync                 # push artifacts into the VM (+ the documented ACME patch)
#   ./drive.sh baseline             # CP0 report + PROVE the probes can see an open port
#   ./drive.sh reboot   CP1|..|CP4  # cutover→checkpoint, REBOOT, assert the state is boot-safe
#   ./drive.sh rollback CP1|..|CP4  # cutover→checkpoint, rollback.sh, assert all 4 restored
#   ./drive.sh full                 # complete cutover, assert, then B3/cert tests
#   ./drive.sh reset                # back to CP0 (independent of rollback.sh)
set -uo pipefail
VM="${VM:-turnrig}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # deploy/media/turn-443
REMOTE=/opt/turn-443
say() { echo; echo "=== $* ==="; }
vm()  { limactl shell "$VM" -- sudo bash -c "$1"; }

# The ONE documented delta between the shipped artifact and what the rig runs: both Caddyfiles
# are pointed at the local Pebble CA. Everything structural under test (https_port 8443, the
# proxy_protocol listener wrapper, disable_tlsalpn_challenge, the kept turn block) is verbatim.
# Each substitution is asserted — a silently-missed patch would leave the rig reaching for the
# real Let's Encrypt and INV-4 would prove nothing.
sync_artifacts() {
  say "syncing artifacts into $VM:$REMOTE"
  vm "mkdir -p $REMOTE $REMOTE/rehearsal"
  for f in haproxy.cfg Caddyfile.mux cutover.sh rollback.sh haproxy-cert-sync.sh \
           haproxy-cert-sync.service haproxy-cert-sync.timer; do
    limactl copy "$REPO_DIR/$f" "$VM:/tmp/$f" >/dev/null 2>&1 || { echo "copy failed: $f"; exit 1; }
    vm "install -m 0755 /tmp/$f $REMOTE/$f"
  done
  for f in checks.sh reset.sh; do
    limactl copy "$REPO_DIR/rehearsal/$f" "$VM:/tmp/$f" >/dev/null 2>&1
    vm "install -m 0755 /tmp/$f $REMOTE/rehearsal/$f"
  done
  vm "python3 - <<'PY'
p='$REMOTE/Caddyfile.mux'; s=open(p).read()
subs=[('{\n    https_port 8443',
       '{\n    acme_ca https://127.0.0.1:14000/dir\n    acme_ca_root /opt/pebble/endpoint-cert.pem\n    https_port 8443'),
      ('        issuer acme {\n            disable_tlsalpn_challenge',
       '        issuer acme {\n            dir https://127.0.0.1:14000/dir\n            trusted_roots /opt/pebble/endpoint-cert.pem\n            disable_tlsalpn_challenge')]
for old,new in subs:
    assert old in s, 'REHEARSAL PATCH MISSED: '+old[:50]
    s=s.replace(old,new,1)
open(p,'w').write(s)
print('  Caddyfile.mux patched for Pebble (2/2 substitutions asserted)')
PY"
  vm "caddy validate --config $REMOTE/Caddyfile.mux --adapter caddyfile >/dev/null 2>&1 && echo '  Caddyfile.mux validates' || echo '  WARN Caddyfile.mux failed validate'"
  # NOTE: `haproxy -c` stats the turn PEM, so it legitimately FAILS before cutover has built it.
  # (That dependency is itself INV-4's teeth: no PEM => haproxy cannot even start.) Report which
  # of the two situations we are in rather than printing a scary-but-expected warning.
  vm "if haproxy -c -f $REMOTE/haproxy.cfg >/dev/null 2>&1; then echo '  haproxy.cfg config-check OK'; \
      elif [ ! -s /etc/haproxy/certs/${TURN_DOMAIN:-turn.enspyr.co}.pem ]; then echo '  haproxy.cfg config-check deferred (turn PEM not built yet — expected pre-cutover)'; \
      else echo '  WARN haproxy.cfg config-check FAILED with a PEM present'; fi"
}

report() { vm "cd $REMOTE/rehearsal && source ./checks.sh && state_report && assert_safety '$1' && echo \"  RESULT pass=\$PASS fail=\$FAIL\" && [ \$FAIL -eq 0 ]"; }

run_cutover() { # run_cutover [checkpoint]
  local stop="${1:-}"
  vm "cd $REMOTE && OFF443_PROVEN=1 REHEARSAL=1 CUTOVER_STOP_AFTER='$stop' CADDY_CERT_DIR=/opt/turncerts bash cutover.sh 2>&1 | tail -25; exit \${PIPESTATUS[0]}"
}

wait_for_vm() {
  for _ in $(seq 1 60); do
    limactl shell "$VM" -- true >/dev/null 2>&1 && { sleep 8; return 0; }
    sleep 3
  done
  echo "VM did not come back"; return 1
}

case "${1:-}" in
  sync) sync_artifacts ;;

  baseline)
    say "CP0 baseline"
    # Validate the probe POSITIVELY first: at CP0 :5349 is genuinely open to the outside, so a
    # probe that cannot see it is broken and every later "closed" would be a false green.
    vm "cd $REMOTE/rehearsal && source ./checks.sh && { ext_tcp_open 5349 && echo '  PROBE VALID: external vantage sees :5349 OPEN at CP0' || echo '  PROBE BROKEN: cannot see an open :5349 — negative results would be meaningless'; }"
    report CP0
    ;;

  reset) vm "cd $REMOTE/rehearsal && bash reset.sh" ;;

  reboot)
    CP="${2:?checkpoint required}"
    say "REBOOT TEST at $CP"
    # NEVER swallow reset's exit code: a silently-failed reset hands the next test a poisoned
    # rig and its result becomes meaningless (this bit once already).
    vm "cd $REMOTE/rehearsal && bash reset.sh" >/dev/null || { echo "RESET FAILED — aborting test"; exit 1; }
    # "done" = reboot the COMPLETED cutover (the steady state must also survive a reboot).
    if [ "$CP" = done ]; then
      run_cutover ""; rc=$?
      [ $rc -eq 0 ] || { echo "full cutover failed with $rc"; exit 1; }
    else
      run_cutover "$CP"; rc=$?
      [ $rc -eq 99 ] || { echo "expected checkpoint stop (99), got $rc"; exit 1; }
    fi
    say "state BEFORE reboot"; report "$CP pre-reboot"
    say "rebooting the guest"
    limactl shell "$VM" -- sudo systemctl reboot >/dev/null 2>&1 || true
    sleep 10; wait_for_vm || exit 1
    say "state AFTER reboot — this is INV-3"
    report "$CP post-reboot"
    ;;

  rollback)
    CP="${2:?checkpoint required}"
    say "ROLLBACK TEST from $CP"
    # NEVER swallow reset's exit code: a silently-failed reset hands the next test a poisoned
    # rig and its result becomes meaningless (this bit once already).
    vm "cd $REMOTE/rehearsal && bash reset.sh" >/dev/null || { echo "RESET FAILED — aborting test"; exit 1; }
    vm "sha256sum /etc/caddy/Caddyfile /home/ubuntu/apps/livekit/livekit.yaml > /tmp/pre-cutover.sha"
    if [ "$CP" = done ]; then
      run_cutover ""; rc=$?
      [ $rc -eq 0 ] || { echo "full cutover failed with $rc"; exit 1; }
    else
      run_cutover "$CP"; rc=$?
      [ $rc -eq 99 ] || { echo "expected checkpoint stop (99), got $rc"; exit 1; }
    fi
    say "running rollback.sh"
    vm "cd $REMOTE && CADDY_CERT_DIR=/opt/turncerts bash rollback.sh 2>&1 | tail -20"
    say "did all four artifacts come back?"
    vm "sha256sum -c /tmp/pre-cutover.sha 2>&1 | sed 's/^/  /'"
    report "$CP post-rollback"
    say "IDEMPOTENCY — running rollback.sh a second time"
    vm "cd $REMOTE && CADDY_CERT_DIR=/opt/turncerts bash rollback.sh >/dev/null 2>&1; echo \"  second run exit=\$?\""
    report "$CP post-rollback-x2"
    ;;

  fault-livekit)
    # INV-9: if LiveKit cannot come back after the external_tls flip, the cutover must ROLL BACK,
    # not carry on. The earlier false-green was "no TLS cert seen == plaintext" — a DEAD socket
    # also presents no cert, so a bootlooping LiveKit could have been read as success.
    say "FAULT — LiveKit cannot boot after the flip (INV-9)"
    vm "cd $REMOTE/rehearsal && bash reset.sh" >/dev/null || { echo "RESET FAILED"; exit 1; }
    vm "printf 'rtc:\n  tcp_port: not-a-number\n' >> /home/ubuntu/apps/livekit/livekit.yaml; echo '  injected an invalid livekit.yaml'"
    run_cutover ""; rc=$?
    echo "  cutover exit=$rc (expected NON-zero: it must refuse to finish)"
    say "did it auto-roll-back to a safe state?"
    report "post-auto-rollback"
    ;;

  full)
    say "FULL CUTOVER"
    # NEVER swallow reset's exit code: a silently-failed reset hands the next test a poisoned
    # rig and its result becomes meaningless (this bit once already).
    vm "cd $REMOTE/rehearsal && bash reset.sh" >/dev/null || { echo "RESET FAILED — aborting test"; exit 1; }
    run_cutover ""; rc=$?
    echo "cutover exit=$rc"
    report "post-cutover"
    ;;

  *) grep '^#   \./drive.sh' "$0" | sed 's/^# //' ;;
esac
