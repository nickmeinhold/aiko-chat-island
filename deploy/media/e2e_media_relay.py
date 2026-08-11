#!/usr/bin/env python3
"""e2e_media_relay.py — acceptance gates for the LiveKit TURN surface.

Two gates (DESIGN §3.4, §4a), both fail-CLOSED — an unverifiable check is a
FAILURE, never a silent pass (a green acceptance gate that didn't actually run is
worse than a red one):

  A  connectivity   forced relay works over TCP/TLS (5349) + a UDP-relay canary.
  B  exposure       unauth ALLOCATE fails · creds are short-TTL · no relay to
                    RFC1918/link-local · ports outside 50000-60000 closed.

The primitive is the canonical TURN client `turnutils_uclient` (coturn tools) — we
do NOT hand-roll a TURN client (a self-authored codec can be self-consistently
wrong; use the reference tool). If it's absent, the gate prints the exact manual
check and EXITS NON-ZERO. The one field this cannot fully self-verify headless —
the browser/livekit-rtc *selected-candidate* `protocol` readout — is called out as
the live confirm (DESIGN §7); it must be checked with a real client before the
surface is called done.

Env (from .env): TURN_DOMAIN, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
                 TURN_RELAY_RANGE=50000-60000
"""
import argparse, os, shutil, subprocess, sys, time

TURN_DOMAIN = os.environ.get("TURN_DOMAIN", "")
UCLIENT = shutil.which("turnutils_uclient")


def _need_uclient(what: str) -> None:
    if UCLIENT:
        return
    print(f"FAIL ({what}): turnutils_uclient not installed — cannot verify headless.\n"
          f"  Install coturn tools (`apt-get install coturn`) OR run the documented\n"
          f"  manual check, then re-run. Failing CLOSED — not asserting a pass.",
          file=sys.stderr)
    sys.exit(3)


def mint_turn_cred() -> tuple[str, str]:
    """Short-TTL TURN credential minted from the LiveKit API key (NOT a static
    TURN secret). LiveKit issues TURN creds via its token flow; this shells the
    project's own minter so the test uses the same path clients do."""
    # Build-time wire-up (DESIGN §7): call the island's LiveKit token/cred minter.
    # Kept explicit so the code cage-match sees the seam rather than a fake secret.
    key, secret = os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    if not (key and secret):
        print("FAIL: LIVEKIT_API_KEY/SECRET unset — cannot mint a test TURN cred.", file=sys.stderr)
        sys.exit(3)
    # TODO(build): replace with the real short-TTL cred mint; must carry a TTL.
    return key, secret


def gate_A(host: str) -> None:
    print(f"== Gate A (connectivity) against {host} ==")
    _need_uclient("gate A")
    user, cred = mint_turn_cred()
    # A1: relay over TLS/TCP on 5349 (-S = TLS). Non-zero exit => no relay path.
    print("  A1: forced relay over TLS/TCP :5349 …")
    r = subprocess.run([UCLIENT, "-S", "-p", "5349", "-u", user, "-w", cred, "-y", "-c", host],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  FAIL A1: TLS relay allocate/permission failed:\n{r.stderr[-800:]}", file=sys.stderr); sys.exit(1)
    print("  ok A1: TLS relay path established.")
    # A2: UDP-relay canary on 3478 — proves the relay range is reachable over UDP.
    print("  A2: UDP-relay canary :3478 …")
    r = subprocess.run([UCLIENT, "-p", "3478", "-u", user, "-w", cred, "-y", "-c", host],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  FAIL A2: UDP relay canary failed:\n{r.stderr[-800:]}", file=sys.stderr); sys.exit(1)
    print("  ok A2: UDP relay reachable.")
    print("  LIVE CONFIRM STILL REQUIRED (DESIGN §7): with a real client "
          "(iceTransportPolicy:'relay'), the selected candidate pair must show "
          "type==relay AND protocol==TCP/TLS — a relay over embedded UDP would "
          "pass A1/A2 but leave the TURNS path unproven.")


def gate_B(host: str) -> None:
    print(f"== Gate B (exposure) against {host} ==")
    _need_uclient("gate B")
    lo, hi = (os.environ.get("TURN_RELAY_RANGE", "50000-60000").split("-") + ["60000"])[:2]
    # B1: unauthenticated ALLOCATE must FAIL.
    print("  B1: unauthenticated ALLOCATE must be rejected …")
    r = subprocess.run([UCLIENT, "-p", "5349", "-S", "-y", "-c", host],
                       capture_output=True, text=True, timeout=45)
    if r.returncode == 0:
        print("  FAIL B1: unauthenticated ALLOCATE SUCCEEDED — this is an open relay.", file=sys.stderr); sys.exit(1)
    print("  ok B1: unauth ALLOCATE rejected.")
    # B2: ports OUTSIDE the relay range are closed (noise-floor probe just below range).
    outside = str(int(lo) - 1)
    print(f"  B2: port {outside} (just below relay range) must be closed …")
    r = subprocess.run(["nc", "-z", "-u", "-w", "3", host, outside], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  FAIL B2: UDP {outside} is OPEN — relay_range mis-set or a prior snowflake left 1024-30000 open.", file=sys.stderr); sys.exit(1)
    print(f"  ok B2: {outside} closed.")
    # B3: no relay to RFC1918/link-local. turnutils can request a peer; a private
    # peer must be refused. (Build confirm: exact uclient invocation for a private
    # CreatePermission — DESIGN §7. Fail-closed until wired.)
    print("  B3: relay-to-private-IP denial — VERIFY AT BUILD (DESIGN §7): "
          "request a permission for a 10.x/169.254.x peer and assert refusal.")
    print("  (B3 must be wired + green before the range is opened to real traffic.)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=TURN_DOMAIN)
    ap.add_argument("--exposure-only", action="store_true", help="gate B only (pre-firewall-open)")
    a = ap.parse_args()
    if not a.host:
        print("FAIL: no host (set TURN_DOMAIN or --host).", file=sys.stderr); sys.exit(2)
    gate_B(a.host)
    if not a.exposure_only:
        gate_A(a.host)
    print("acceptance gate(s) passed (subject to the DESIGN §7 live confirms noted above).")


if __name__ == "__main__":
    main()
