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
#   ./drive.sh refresh-ca           # re-fetch Pebble's root + re-issue certs (after any reboot)
set -uo pipefail
VM="${VM:-turnrig}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # deploy/media/turn-443
REMOTE=/opt/turn-443
say() { echo; echo "=== $* ==="; }
# The rig's certs come from a local Pebble CA, so chain verification against a REAL trust store
# is meaningless here. turn_tls_ok is fail-closed on that by default (a chain production clients
# cannot verify is a chain that does not work), so the rig declares the opt-out explicitly —
# once, visibly, rather than by weakening the shared assertion for everyone.
vm()  { limactl shell "$VM" -- sudo bash -c "export TURN_ALLOW_UNVERIFIED_CHAIN=1; $1"; }

# The ONE documented delta between the shipped artifact and what the rig runs: both Caddyfiles
# are pointed at the local Pebble CA. Everything structural under test (https_port 8443, the
# proxy_protocol listener wrapper, disable_tlsalpn_challenge, the kept turn block) is verbatim.
# Each substitution is asserted — a silently-missed patch would leave the rig reaching for the
# real Let's Encrypt and INV-4 would prove nothing.
sync_artifacts() {
  say "syncing artifacts into $VM:$REMOTE"
  vm "mkdir -p $REMOTE $REMOTE/rehearsal"
  for f in haproxy.cfg.tmpl Caddyfile.mux cutover.sh rollback.sh migrate-to-passthrough.sh; do
    limactl copy "$REPO_DIR/$f" "$VM:/tmp/$f" >/dev/null 2>&1 || { echo "copy failed: $f"; exit 1; }
    vm "install -m 0755 /tmp/$f $REMOTE/$f"
  done
  # lib/turn-assert.sh is the single door for the shared assertions — the scripts source it at
  # runtime, so a sync that forgets it produces a "command not found" at deploy time, not a
  # silently weaker check. Ship it first.
  vm "mkdir -p $REMOTE/lib"
  limactl copy "$REPO_DIR/lib/turn-assert.sh" "$VM:/tmp/turn-assert.sh" >/dev/null 2>&1 || { echo "copy failed: lib/turn-assert.sh"; exit 1; }
  vm "install -m 0644 /tmp/turn-assert.sh $REMOTE/lib/turn-assert.sh"
  for f in checks.sh reset.sh; do
    limactl copy "$REPO_DIR/rehearsal/$f" "$VM:/tmp/$f" >/dev/null 2>&1
    vm "install -m 0755 /tmp/$f $REMOTE/rehearsal/$f"
  done
  # cert-restart is now LOAD-BEARING (LiveKit holds the cert and cannot hot-reload it), so the
  # rig must exercise the real script, not a stand-in. FAULT 1 runs it end to end.
  vm "mkdir -p /opt/media/lib"
  for f in cert-restart.sh served-cert-alarm.sh; do
    limactl copy "$REPO_DIR/../$f" "$VM:/tmp/$f" >/dev/null 2>&1 || { echo "copy failed: $f"; exit 1; }
    vm "install -m 0755 /tmp/$f /opt/media/$f"
  done
  limactl copy "$REPO_DIR/../lib/cert-pair.sh" "$VM:/tmp/cert-pair.sh" >/dev/null 2>&1
  vm "install -m 0644 /tmp/cert-pair.sh /opt/media/lib/cert-pair.sh"
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
  # Under passthrough the config references NO cert, so `haproxy -c` is unconditionally
  # checkable — no more "deferred until the PEM exists" branch, which was itself a symptom:
  # a config you cannot validate until a side-effect has run is a config with a hidden
  # dependency. Render the template first, exactly as cutover.sh will.
  vm "sed 's/@@TURN_DOMAIN@@/${TURN_DOMAIN:-turn.enspyr.co}/g' $REMOTE/haproxy.cfg.tmpl > $REMOTE/haproxy.cfg.rendered
      if grep -q '@@' $REMOTE/haproxy.cfg.rendered; then echo '  WARN unrendered placeholder in haproxy.cfg'; fi
      if haproxy -c -f $REMOTE/haproxy.cfg.rendered >/dev/null 2>&1; then echo '  haproxy.cfg (rendered) config-check OK'; \
      else echo '  WARN haproxy.cfg config-check FAILED'; haproxy -c -f $REMOTE/haproxy.cfg.rendered 2>&1 | tail -3; fi"
}

report() { vm "cd $REMOTE/rehearsal && source ./checks.sh && state_report && assert_safety '$1' && echo \"  RESULT pass=\$PASS fail=\$FAIL\" && [ \$FAIL -eq 0 ]"; }

# assert_safety answers "is this state SAFE to be in", which is deliberately independent of
# "did the thing we ran WORK". Those are different questions and conflating them is a fail-open:
# a cutover that aborted at line 1 leaves a perfectly safe CP0 box, and reporting that as
# `post-cutover pass=4 fail=0` is the F1 class of bug one layer up — the gate certifying the
# very outcome it exists to detect. Caught on this rig 2026-08-14, by exactly that output.
# So SUCCESS is asserted separately, by who owns :443.
assert_muxed()   { vm "[ -n \"\$(ss -tlnpH 'sport = :443' | grep haproxy)\" ]" \
                     && echo "  SUCCESS haproxy owns :443 (the cutover actually happened)" \
                     || { echo "  FAIL :443 is not owned by haproxy — the cutover did NOT take effect"; return 1; }; }
assert_unmuxed() { vm "[ -n \"\$(ss -tlnpH 'sport = :443' | grep caddy)\" ]" \
                     && echo "  SUCCESS caddy owns :443 (the rollback actually happened)" \
                     || { echo "  FAIL :443 is not owned by caddy — the rollback did NOT take effect"; return 1; }; }

run_cutover() { # run_cutover [checkpoint]
  local stop="${1:-}"
  vm "cd $REMOTE && OFF443_PROVEN=1 CERT_RENEWAL_OWNER=timer REHEARSAL=1 CUTOVER_STOP_AFTER='$stop' TURN_DOMAIN='${TURN_DOMAIN:-turn.enspyr.co}' CADDY_CERT_DIR=/opt/turncerts bash cutover.sh 2>&1 | tail -25; exit \${PIPESTATUS[0]}"
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

  # Pebble mints a NEW issuance root every time its container starts, so any guest reboot (or
  # a docker restart) silently invalidates every cert Caddy issued under the previous root AND
  # the copy in the system trust store. Everything then fails with "unable to get local issuer
  # certificate" — which looks exactly like a server fault and is not one. This bit twice in one
  # session; the second time cost a diagnosis detour mid-rehearsal. It is a rig-maintenance
  # command, not a test, so it lives here rather than in reset.sh (which must stay independent
  # of the artifact under test AND cheap enough to run between every case).
  refresh-ca)
    TD="${TURN_DOMAIN:-turn.enspyr.co}"
    say "refreshing the Pebble issuance root + re-issuing all certs ($TD)"
    vm "curl -sk --max-time 5 https://127.0.0.1:15000/roots/0 -o /opt/pebble/issuance-root.pem
        grep -q 'BEGIN CERTIFICATE' /opt/pebble/issuance-root.pem || { echo '  FATAL: could not fetch Pebble root (is the container up?)'; exit 1; }
        cp /opt/pebble/issuance-root.pem /usr/local/share/ca-certificates/pebble-issuance-root.crt
        update-ca-certificates >/dev/null 2>&1
        # /opt/turncerts points at the PER-DOMAIN dir for turn.<domain>, so wiping only that
        # re-issues turn and leaves chat/livekit still signed by the SUPERSEDED root — which is
        # how the first attempt reported 'turn ok, chat still failing'. Clear the whole ACME
        # issuer tree so EVERY name is re-issued under the current root.
        STORE=\$(readlink -f /opt/turncerts); ACME_DIR=\$(dirname \$STORE)
        rm -rf \$ACME_DIR/*
        systemctl restart caddy
        for i in \$(seq 1 60); do [ -s \"\$STORE/${TD}.crt\" ] && break; sleep 2; done
        docker restart livekit >/dev/null; sleep 8
        echo '  re-issued; verifying chain end-to-end:'
        curl -sS -o /dev/null -w '    chat  -> %{http_code}\n' --max-time 8 https://chat.${TD#turn.} 2>&1 || echo '    chat  -> STILL FAILING'
        echo | openssl s_client -connect 127.0.0.1:5349 -servername ${TD} 2>&1 | grep -m1 'Verify return code' | sed 's/^/    turn  -> /'"
    ;;

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
    # assert_muxed too (Tesla round 7): INV-3 is the claim that the CUT-OVER state is
    # boot-correct, and `report` only answers "is this state safe". A box that came back with
    # Caddy on :443 is perfectly safe and has un-run the cutover — which is the exact
    # conflation named in RESULTS.md, found for `full`, and left here in the reboot path.
    if [ "$CP" = "done" ] || [ "$CP" = "CP4" ]; then
      report "$CP post-reboot" && assert_muxed
    else
      report "$CP post-reboot"
    fi
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
    # livekit.yaml stays in this snapshot on purpose even though the cutover never touches it:
    # "the file we promise not to modify is byte-identical afterwards" is a claim worth checking,
    # not assuming. It is the cheapest possible regression test for the whole shape.
    say "did both artifacts come back byte-identical (and livekit.yaml stay untouched)?"
    vm "sha256sum -c /tmp/pre-cutover.sha 2>&1 | sed 's/^/  /'"
    report "$CP post-rollback" && assert_unmuxed
    say "IDEMPOTENCY — running rollback.sh a second time"
    vm "cd $REMOTE && CADDY_CERT_DIR=/opt/turncerts bash rollback.sh >/dev/null 2>&1; echo \"  second run exit=\$?\""
    report "$CP post-rollback-x2" && assert_unmuxed
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
    [ $rc -eq 0 ] || { echo "FULL CUTOVER FAILED (exit $rc) — not reporting a state as if it succeeded"; exit 1; }
    report "post-cutover" && assert_muxed
    ;;

  *) grep '^#   \./drive.sh' "$0" | sed 's/^# //' ;;
esac
