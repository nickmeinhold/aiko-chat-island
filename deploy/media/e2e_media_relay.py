#!/usr/bin/env python3
"""e2e_media_relay.py — acceptance gates for the LiveKit TURN surface.

Two gates (DESIGN §3.4, §4a), both fail-CLOSED. "Fail-closed" here is literal: any
check that is not-yet-wired or cannot produce POSITIVE evidence returns non-zero, so
standup.sh can NEVER open the public firewall range on a false green (round-1
Carnot/Tesla: a B3 that only `print`s, and a B1 that reads any error as "rejected",
were fail-OPEN on the most critical exposure checks).

  A  connectivity   forced relay works over TCP/TLS (5349) + a UDP-relay canary.
  B  exposure       unauth ALLOCATE POSITIVELY rejected · creds short-TTL LiveKit-issued
                    · no relay to RFC1918/link-local · ports outside range closed.

The primitive is the canonical `turnutils_uclient` (coturn tools; server address is
the trailing POSITIONAL arg) — we do NOT hand-roll a TURN client. Confirm the exact
turnutils_uclient flags against the installed build at Phase-2 (DESIGN §7); a wrong
flag fails CLOSED (block), never opens the firewall. Some sub-checks (real cred mint,
B3 private-IP denial) are §7 wire-ups; until wired they BLOCK, they do not wave through.

Env: TURN_DOMAIN, LIVEKIT_API_KEY/SECRET, TURN_RELAY_START/END,
     LIVEKIT_TURN_CRED_CMD (a command that prints `user\\tcredential\\tttl_seconds`
     for a short-TTL LiveKit-issued TURN credential — the real client path),
     TURN_B3_PRIVATE_DENY_CMD (exits 0 iff a private-IP relay permission is refused).
"""
import argparse, os, shutil, subprocess, sys

TURN_DOMAIN = os.environ.get("TURN_DOMAIN", "")
UCLIENT = shutil.which("turnutils_uclient")
# Positive auth-reject evidence for B1. NOTE (DESIGN §7, round-2 Tesla): these are
# coupled to coturn/pion English; pin them to a GOLDEN stderr captured from the real
# livekit-server v1.13.5 + turnutils_uclient build during Phase-2 standup. Until
# then B1 fails CLOSED if none match (safe: never opens on unproven rejection).
AUTH_REJECT_MARKERS = ("401", "403", "unauthorized", "forbidden", "allocate error", "wrong credentials")


def block(what: str, why: str) -> "NoReturn":
    print(f"BLOCKED ({what}): {why}\n  Failing CLOSED — the firewall range must NOT open on this.", file=sys.stderr)
    sys.exit(3)


def need_uclient(what: str) -> None:
    if not UCLIENT:
        block(what, "turnutils_uclient not installed (apt-get install coturn). Cannot verify headless.")


def mint_turn_cred() -> tuple:
    """Short-TTL LiveKit-issued TURN credential — the REAL client path, not the API
    key/secret (round-1: returning the API pair tests the wrong credential class and
    can't prove relay). Wire LIVEKIT_TURN_CRED_CMD to the island's minter."""
    cmd = os.environ.get("LIVEKIT_TURN_CRED_CMD", "")
    if not cmd:
        block("cred mint", "LIVEKIT_TURN_CRED_CMD not set — the short-TTL TURN cred path (DESIGN §7) is not wired. "
                           "The API key/secret is NOT a TURN credential; refusing to fake it.")
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    parts = out.stdout.strip().split("\t")
    if out.returncode != 0 or len(parts) < 3:
        block("cred mint", f"cred command failed / bad output (want user\\tcred\\tttl): {out.stderr[-300:]}")
    user, cred, ttl = parts[0], parts[1], parts[2]
    if not ttl.isdigit() or not (0 < int(ttl) <= 86400):
        block("cred mint", f"TTL '{ttl}' not a sane short TTL (0<ttl<=86400) — creds must be short-lived.")
    return user, cred


def gate_A(host: str) -> None:
    print(f"== Gate A (connectivity) against {host} ==")
    need_uclient("gate A")
    user, cred = mint_turn_cred()
    print("  A1: forced relay over TLS/TCP :5349 …")
    r = subprocess.run([UCLIENT, "-S", "-p", "5349", "-u", user, "-w", cred, "-y", host],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        block("A1", f"TLS relay allocate/permission failed:\n{r.stderr[-600:]}")
    print("  ok A1: TLS relay path established.")
    print("  A2: UDP-relay canary :3478 …")
    r = subprocess.run([UCLIENT, "-p", "3478", "-u", user, "-w", cred, "-y", host],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        block("A2", f"UDP relay canary failed:\n{r.stderr[-600:]}")
    print("  ok A2: UDP relay reachable.")
    print("  LIVE CONFIRM STILL REQUIRED (DESIGN §7): with a real client "
          "(iceTransportPolicy:'relay'), the selected candidate pair must show "
          "type==relay AND protocol==TCP/TLS — A1/A2 do not distinguish TURNS from embedded-UDP relay.")


def gate_B(host: str) -> None:
    print(f"== Gate B (exposure) against {host} ==")
    need_uclient("gate B")
    try:
        lo = int(os.environ["TURN_RELAY_START"]); hi = int(os.environ["TURN_RELAY_END"])
    except (KeyError, ValueError):
        block("gate B", "TURN_RELAY_START/END unset or non-numeric — the single-source relay range must be set.")

    # B1: unauthenticated ALLOCATE must be POSITIVELY rejected — a non-zero exit
    # alone is not proof (TLS-down / timeout / bad-host also exit non-zero). Require
    # an auth-reject marker in the tool output (round-1 Tesla: fail-open detector).
    print("  B1: unauthenticated ALLOCATE must be rejected (positive evidence) …")
    r = subprocess.run([UCLIENT, "-p", "5349", "-S", "-y", host], capture_output=True, text=True, timeout=45)
    blob = (r.stdout + r.stderr).lower()
    if r.returncode == 0:
        block("B1", "unauthenticated ALLOCATE SUCCEEDED — this is an open relay.")
    if not any(m in blob for m in AUTH_REJECT_MARKERS):
        block("B1", f"ALLOCATE failed but WITHOUT an auth-reject signal ({'/'.join(AUTH_REJECT_MARKERS)}) — "
                    f"could be TLS/host/timeout, not a real 401. Not accepting as proof:\n{blob[-500:]}")
    print("  ok B1: unauth ALLOCATE positively rejected.")

    # B2: ports outside the relay range closed. `nc -zu` is a WEAK UDP instrument
    # (implementation-defined), so this probes several ports AND is explicitly
    # advisory — it can only FAIL the gate on an open port, never certify closure.
    if not shutil.which("nc"):
        block("B2", "nc not installed — cannot sample outside-range ports. Failing closed.")
    print(f"  B2: sampling ports outside {lo}-{hi} (advisory; can fail, cannot certify) …")
    for p in (lo - 1, hi + 1, 20000):   # neighbours + a mid old-default(1024-30000) sample
        if subprocess.run(["nc", "-z", "-u", "-w", "3", host, str(p)], capture_output=True).returncode == 0:
            block("B2", f"UDP {p} (outside relay range) is OPEN — relay_range mis-set or a snowflake left the old 1024-30000 open.")
    print("  ok B2 (advisory): sampled outside ports not observed open — external multi-port firewall audit still required (DESIGN §7).")

    # B3: no relay to RFC1918/link-local. Not-yet-wired => BLOCK (the open-relay/SSRF
    # class; must be a real assertion before the range opens, never a print).
    b3_cmd = os.environ.get("TURN_B3_PRIVATE_DENY_CMD", "")
    if not b3_cmd:
        block("B3", "private-IP relay-denial check (DESIGN §7) not wired (set TURN_B3_PRIVATE_DENY_CMD to a "
                    "command that requests a permission for a 10.x/169.254.x peer and exits 0 iff REFUSED). "
                    "Refusing to open the range without proving no-relay-to-private-IP.")
    if subprocess.run(b3_cmd, shell=True, capture_output=True, text=True, timeout=45).returncode != 0:
        block("B3", "private-IP relay was NOT refused (or check errored) — open-relay-to-internal risk.")
    print("  ok B3: relay to RFC1918/link-local refused.")


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
    print("acceptance gate(s) passed (A2/B2 external live-confirms still required — DESIGN §7).")


if __name__ == "__main__":
    main()
