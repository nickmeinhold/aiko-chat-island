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
                   B3 no relay to RFC1918/link-local — asserted via the LiveKit VERSION:
                      v1.12.0+ denies restricted (private) peer CIDRs by DEFAULT
                      (RESEARCH §2 / upstream changelog). Replaces the old unwired
                      runtime probe (TURN_B3_PRIVATE_DENY_CMD) that blocked forever.
                      Relies on the shipped default (no permissive CIDR override); the
                      pinned image is the single source of the version.

Env: TURN_DOMAIN, LIVEKIT_URL (wss signaling), LIVEKIT_API_KEY/SECRET,
     TURN_RELAY_START/END, LIVEKIT_IMAGE (optional; else `docker inspect livekit`).
Requires (gate A): a python with livekit + livekit-api + numpy (the box venv).
"""
import argparse, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "e2e_relay_livekit.py")
TURN_DOMAIN = os.environ.get("TURN_DOMAIN", "")
UCLIENT = shutil.which("turnutils_uclient")
# Positive auth-reject evidence for B1. Coupled to coturn/pion English; pin to a GOLDEN
# stderr from the real livekit-server + turnutils build at Phase-2 (DESIGN §7). Until
# then B1 fails CLOSED if none match (safe: never opens on unproven rejection).
AUTH_REJECT_MARKERS = ("401", "403", "unauthorized", "forbidden", "allocate error", "wrong credentials")
MIN_CIDR_DENY_VERSION = (1, 12, 0)   # v1.12.0: restricted-CIDR relay denied by default


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
    if r.returncode != 0 or "RESULT=RELAY_MEDIA_OK" not in out:
        block("A", f"forced relay-only media did NOT round-trip (rc={r.returncode}):\n{out[-900:]}")
    # Fail on ANY all_relay:false (pub OR sub) — a bare `"all_relay": true in out` would
    # be satisfied by one peer's line while the other leaked a non-relay candidate.
    if '"all_relay": false' in out:
        block("A", f"a peer gathered a NON-relay candidate (all_relay:false) — forced-relay policy "
                   f"not honored on both legs:\n{out[-500:]}")
    if '"all_relay": true' not in out:
        block("A", f"no positive all_relay evidence (stats parse failed?) — cannot certify the path was "
                   f"relay-only:\n{out[-500:]}")
    print("  ok A: forced relay-only media round-tripped; every candidate was RELAY (media went through TURN).")
    if "turns:" in out and ":5349" in out:
        print("  NOTE: a TLS/5349 relay candidate was used — TLS fallback may now work; re-check the KNOWN GAP doc.")
    else:
        print("  NOTE: UDP/3478 relay proven. TLS/5349 relay remains a KNOWN GAP (not advertised to clients; "
              "DESIGN §4a TLS assertion deferred to the external_tls task).")


def _running_livekit_version() -> tuple:
    ref = os.environ.get("LIVEKIT_IMAGE", "")
    if not ref and shutil.which("docker"):
        r = subprocess.run(["docker", "inspect", "livekit", "--format", "{{.Config.Image}}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            ref = r.stdout.strip()
    m = re.search(r":v?(\d+)\.(\d+)\.(\d+)", ref or "")
    return ((int(m.group(1)), int(m.group(2)), int(m.group(3))), ref) if m else (None, ref)


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

    # B3: no relay to RFC1918/link-local — asserted via the LiveKit VERSION default
    # (v1.12.0+ denies restricted peer CIDRs by default). Replaces the old unwired
    # runtime probe. The pinned image is the single source of truth for the version.
    print("  B3: relay-to-private-IP denied by the LiveKit version default …")
    ver, ref = _running_livekit_version()
    if ver is None:
        block("B3", f"cannot determine the LiveKit version from '{ref}' — set LIVEKIT_IMAGE to the pinned "
                    f"ref, or run where `docker inspect livekit` works. Refusing to certify RFC1918 relay-deny.")
    if ver < MIN_CIDR_DENY_VERSION:
        block("B3", f"LiveKit {ref} < v{'.'.join(map(str, MIN_CIDR_DENY_VERSION))} does NOT deny restricted-CIDR "
                    f"relay by default — SSRF-shaped open-relay-to-internal risk. Pin a newer image.")
    print(f"  ok B3: LiveKit {ref} >= v{'.'.join(map(str, MIN_CIDR_DENY_VERSION))} "
          f"(restricted/private-CIDR relay denied by default).")


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
