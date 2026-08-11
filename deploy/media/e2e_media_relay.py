#!/usr/bin/env python3
"""e2e_media_relay.py — acceptance gates for the LiveKit TURN surface.

Two gates (DESIGN §3.4, §4a), both fail-CLOSED. "Fail-closed" is literal: any check
that cannot produce POSITIVE evidence returns non-zero, so standup.sh can NEVER open
the public firewall range on a false green.

  A  connectivity  A REAL livekit-rtc client (e2e_relay_livekit.py) forces relay-only
                   ICE and confirms synthetic video round-trips through a TURN
                   allocation. This is the only headless test that WORKS: LiveKit's
                   embedded TURN is SESSION-BOUND — there is no standalone TURN
                   credential to hand turnutils_uclient, so the previous turnutils gate
                   A was wrong-premised (it required LIVEKIT_TURN_CRED_CMD, which cannot
                   exist). The client gets its session-bound cred by joining a room.
                   Asserts: RESULT=RELAY_MEDIA_OK AND every gathered ICE candidate was
                   candidate_type=RELAY (all_relay=true) — media had no path but the
                   TURN allocation. Proves the UDP/3478 relay path.

                   KNOWN GAP — TLS/5349 relay is NOT asserted (proven non-functional
                   2026-08-11): LiveKit advertises only the UDP TURN to clients
                   (boot log turn.externalTLS:false; a forced-relay client gathers a
                   single UDP relay candidate; with UDP blocked the peer connection
                   times out — wait_pc_connection). The :5349 cert is valid; the relay
                   ADVERTISEMENT is the gap. DESIGN §4a's TLS-relay assertion is DEFERRED
                   to the external_tls config task, NOT gated here. Do not re-add a
                   TLS-relay assertion until that task proves it — a gate must assert
                   what the system does, not what we wish it did.

  B  exposure      Before the firewall opens to non-test traffic:
                   B1 unauthenticated ALLOCATE POSITIVELY rejected (auth-reject marker,
                      not merely a non-zero exit) — turnutils_uclient, no cred needed
                      for the negative test.
                   B2 ports OUTSIDE the relay range sampled closed (advisory: can fail
                      the gate on an open port, cannot certify closure).
                   B3 no relay to RFC1918/link-local — a BEHAVIORAL, packet-level probe
                      (b3_relay_probe.py), NOT a version/config proxy. It extracts the
                      SFU's session-bound TURN cred from the raw signaling JoinResponse
                      (the same session-bound cred gate A relies on, read off the wire —
                      livekit-rtc never surfaces it), allocates a relay, then issues
                      CreatePermission for a PUBLIC control + a representative SENTINEL
                      per SSRF-critical private range (the set is MANDATORY/hard-coded,
                      not env-shrinkable), requiring 200 for the control and 403 for
                      every sentinel. Sampled, not an exhaustive range proof. A version
                      proxy was the prior stopgap; the behavioral probe
                      supersedes it AND is more truthful (it found LiveKit's default deny
                      does NOT cover 100.64/10 CGNAT — task #6, excluded from the set).
                      Runs in the exposure phase: needs :443 (join) + :3478 (TURN
                      control), NOT the relay range.

Env: TURN_DOMAIN, LIVEKIT_URL (wss signaling), LIVEKIT_API_KEY/SECRET, TURN_RELAY_START/END.
Requires: a python with livekit + livekit-api + numpy (gate A) + websockets + aioice
     (gate B3) — the box venv.
"""
import argparse, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "e2e_relay_livekit.py")
B3_PROBE = os.path.join(HERE, "b3_relay_probe.py")
TURN_DOMAIN = os.environ.get("TURN_DOMAIN", "")
UCLIENT = shutil.which("turnutils_uclient")
# Positive auth-reject evidence for B1. Coupled to coturn/pion English; pin to a GOLDEN
# stderr from the real livekit-server + turnutils build at Phase-2 (DESIGN §7). Until
# then B1 fails CLOSED if none match (safe: never opens on unproven rejection).
AUTH_REJECT_MARKERS = ("401", "403", "unauthorized", "forbidden", "allocate error", "wrong credentials")


def block(what: str, why: str) -> "NoReturn":
    print(f"BLOCKED ({what}): {why}\n  Failing CLOSED — the firewall range must NOT open on this.", file=sys.stderr)
    sys.exit(3)


def need_uclient(what: str) -> None:
    if not UCLIENT:
        block(what, "turnutils_uclient not installed (apt-get install coturn). Cannot verify headless.")


def gate_A(host: str) -> None:
    """Connectivity: forced relay-only media round-trip via the livekit-rtc harness."""
    print(f"== Gate A (connectivity) — forced relay-only media via livekit-rtc ==")
    url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        block("gate A", "LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET required for the relay client.")
    if not os.path.exists(HARNESS):
        block("gate A", f"missing {HARNESS}")
    env = {**os.environ, "LK_URL": url, "LK_API_KEY": key, "LK_API_SECRET": secret}
    try:
        r = subprocess.run([sys.executable, HARNESS], env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        block("A", "relay harness timed out (no media round-trip within 180s) — relay path down.")
    out = r.stdout + r.stderr
    if r.returncode != 0 or not re.search(r"(?m)^RESULT=RELAY_MEDIA_OK\b", out):
        block("A", f"forced relay-only media did NOT round-trip (rc={r.returncode}):\n{out[-900:]}")
    # The harness OWNS the dual-leg relay-only invariant and exits non-zero unless BOTH
    # pub and sub gathered candidates that were ALL relay. We confirm its structured
    # RELAY_ASSERT line agrees — a machine contract, not a stdout grep (cage-match #128:
    # a bare `"all_relay": true in out` was satisfied by one leg while the other leaked).
    # Line-anchored + require EXACTLY ONE (cage-match #128: a non-anchored first-match
    # regex over merged stdout/stderr could be satisfied by a stray/duplicate line
    # before a later failing assertion).
    asserts = re.findall(r"(?m)^RELAY_ASSERT=(\{.*\})$", out)
    if len(asserts) != 1:
        block("A", f"expected exactly one RELAY_ASSERT line, found {len(asserts)} — cannot confirm both legs "
                   f"relay-only:\n{out[-500:]}")
    try:
        a = json.loads(asserts[0])
    except json.JSONDecodeError:
        block("A", f"unparseable RELAY_ASSERT: {asserts[0][:200]}")
    if not (a.get("result") == "OK" and a.get("pub_all_relay") is True and a.get("sub_all_relay") is True):
        block("A", f"relay-only NOT proven on BOTH legs (pub+sub each all-relay): {a}")
    print("  ok A: forced relay-only media round-tripped; every candidate was RELAY (media went through TURN).")
    if "turns:" in out and ":5349" in out:
        print("  NOTE: a TLS/5349 relay candidate was used — TLS fallback may now work; re-check the KNOWN GAP doc.")
    else:
        print("  NOTE: UDP/3478 relay proven. TLS/5349 relay remains a KNOWN GAP (not advertised to clients; "
              "DESIGN §4a TLS assertion deferred to the external_tls task).")


def gate_B3(host: str) -> None:
    """No relay to RFC1918/link-local — a BEHAVIORAL, packet-level assertion of the
    RUNNING TURN (b3_relay_probe.py), not a version/config proxy. The probe extracts
    the SFU's session-bound TURN cred from the raw signaling JoinResponse, allocates a
    relay, and issues CreatePermission to a public control + the SSRF-critical private
    ranges — requiring the control ALLOWED (200) and every private peer REFUSED (403).
    Its exit code (0 OK / 3 FAIL / 2 BLOCK) is fail-closed; we ALSO parse the single
    structured B3_ASSERT line, a machine contract (mirrors gate A's RELAY_ASSERT)."""
    print("  B3: relay-to-private-IP denied (behavioral CreatePermission probe) …")
    url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        block("B3", "LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET required for the behavioral TURN probe.")
    if not os.path.exists(B3_PROBE):
        block("B3", f"missing {B3_PROBE}")
    env = {**os.environ, "LK_URL": url, "LK_API_KEY": key, "LK_API_SECRET": secret}
    try:
        r = subprocess.run([sys.executable, B3_PROBE], env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        block("B3", "behavioral TURN probe timed out (signaling :443 or TURN :3478 unreachable?) — fail closed.")
    out = r.stdout + r.stderr
    for line in (l for l in out.splitlines() if l.strip()):
        print("   " + line)
    # Exactly-one machine contract (cage-match #128 lesson on gate A: a non-anchored or
    # duplicated assert line can mask a later failure). The probe emits ONE B3_ASSERT.
    asserts = re.findall(r"(?m)^B3_ASSERT=(\{.*\})$", out)
    if len(asserts) != 1:
        block("B3", f"expected exactly one B3_ASSERT line, found {len(asserts)} — cannot certify "
                    f"relay-deny:\n{out[-500:]}")
    try:
        a = json.loads(asserts[0])
    except json.JSONDecodeError:
        block("B3", f"unparseable B3_ASSERT: {asserts[0][:200]}")
    # Require BOTH the fail-closed exit code AND the structured OK verdict — either alone
    # is insufficient (a 0 with a non-OK body, or an OK body on a non-zero exit, is drift).
    if r.returncode != 0 or a.get("result") != "OK":
        block("B3", f"behavioral relay-deny NOT proven (rc={r.returncode}): {a.get('reason', a)}")
    print("  ok B3: behavioral probe — every advertised turn:udp endpoint: allocation "
          "succeeded, public control allowed (before+after), every SSRF-critical private "
          "sentinel's CreatePermission refused (403). Sampled sentinels, not a full-range proof.")


def gate_B(host: str) -> None:
    print(f"== Gate B (exposure) against {host} ==")
    need_uclient("gate B")
    try:
        lo = int(os.environ["TURN_RELAY_START"]); hi = int(os.environ["TURN_RELAY_END"])
    except (KeyError, ValueError):
        block("gate B", "TURN_RELAY_START/END unset or non-numeric — the single-source relay range must be set.")

    # B1: unauthenticated ALLOCATE must be POSITIVELY rejected — a non-zero exit alone
    # is not proof (TLS-down / timeout / bad-host also exit non-zero). Require an
    # auth-reject marker. No credential needed: this is the NEGATIVE path.
    print("  B1: unauthenticated ALLOCATE must be rejected (positive evidence) …")
    r = subprocess.run([UCLIENT, "-p", "5349", "-S", "-y", host], capture_output=True, text=True, timeout=45)
    blob = (r.stdout + r.stderr).lower()
    if r.returncode == 0:
        block("B1", "unauthenticated ALLOCATE SUCCEEDED — this is an open relay.")
    if not any(m in blob for m in AUTH_REJECT_MARKERS):
        block("B1", f"ALLOCATE failed but WITHOUT an auth-reject signal ({'/'.join(AUTH_REJECT_MARKERS)}) — "
                    f"could be TLS/host/timeout, not a real 401. Not accepting as proof:\n{blob[-500:]}")
    print("  ok B1: unauth ALLOCATE positively rejected.")

    # B2: ports outside the relay range closed. `nc -zu` is a WEAK UDP instrument, so
    # this is explicitly advisory — it can only FAIL the gate on an open port.
    if not shutil.which("nc"):
        block("B2", "nc not installed — cannot sample outside-range ports. Failing closed.")
    print(f"  B2: sampling ports outside {lo}-{hi} (advisory; can fail, cannot certify) …")
    for p in (lo - 1, hi + 1, 20000):
        if subprocess.run(["nc", "-z", "-u", "-w", "3", host, str(p)], capture_output=True).returncode == 0:
            block("B2", f"UDP {p} (outside relay range) is OPEN — relay_range mis-set or an old 1024-30000 default left open.")
    print("  ok B2 (advisory): sampled outside ports not observed open — external multi-port audit still required (DESIGN §7).")

    # B3: no relay to RFC1918/link-local — behavioral CreatePermission probe (see gate_B3).
    gate_B3(host)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=TURN_DOMAIN)
    ap.add_argument("--exposure-only", action="store_true", help="gate B only (pre-firewall-open)")
    a = ap.parse_args()
    if not a.host:
        block("args", "no host (set TURN_DOMAIN or --host).")
    gate_B(a.host)
    if not a.exposure_only:
        gate_A(a.host)
    print("acceptance gate(s) passed (B2 external multi-port audit + the TLS-relay gap still tracked — DESIGN §4a/§7).")


if __name__ == "__main__":
    main()
