#!/usr/bin/env python3
"""Behavioral B3: prove the running LiveKit embedded-TURN REFUSES to relay to
RFC1918/link-local peers — a packet-level assertion, not a version/config proxy.

WHY THIS EXISTS (cage-match #128, Carnot+Tesla): the shipped gate B3 asserts a
POLICY proxy (runtime image + official repo + version floor + config key-set),
which is fail-closed but does NOT observe the running server actually deny a
private-peer relay. This harness closes that gap.

THE FALSIFIER THAT SHAPED IT (task #5, run 2026-08-11):
  - livekit-rtc (the high-level Python client) does NOT surface the SFU-issued
    TURN credential: ConnectCallback.Result carries only room/participants; the
    only ice_servers field in room_pb2 is the OUTBOUND RtcConfig; get_rtc_stats()
    exposes the relay candidate url + ICE username_fragment but NOT the TURN
    long-term username/credential. So Tesla's "read the cred off the joined
    session" is impossible through that client.
  - BUT the raw signaling protocol carries it: SignalResponse.join.ice_servers ->
    ICEServer{urls, username, credential} (livekit.protocol.rtc, shipped by the
    livekit-api package). So we speak the signal WebSocket directly, read the
    first JoinResponse, and extract the session-bound TURN credential ourselves.

WHAT IT PROVES, in two halves:
  1) EXTRACT — WS-connect to the SFU signal endpoint with a minted join token,
     read the first SignalResponse.join, pull the session-bound TURN creds.
  2) ASSERT — with those creds, allocate a relay (aioice's TURN client, which
     handles the long-term-cred 401->REALM/NONCE->MESSAGE-INTEGRITY re-auth that
     turnutils botched), then issue explicit CreatePermission requests and read
     the response CODE: 200 = allowed, 403 = refused. A PUBLIC-peer control must
     be ALLOWED (proving permissions work at all) and every PRIVATE peer
     (10.x / 169.254.x) must be REFUSED.

Why not turnutils_uclient (task #5, observed live 2026-08-11): against LiveKit's
session-bound TURN it fires multiple allocations + a channel-bind that 401s and
never isolates a single CreatePermission -> 403; its -y mode self-relays and
exits 0 (looks like a pass, tests nothing). aioice speaks the transaction we
actually need and surfaces the ERROR-CODE cleanly.

FAIL-CLOSED, and note the inversion vs a normal gate: RESULT=OK (rc 0) requires
"cred extracted + allocation succeeded + PUBLIC control ALLOWED + EVERY private
peer REFUSED (403)". If the server ALLOWS a private-peer permission that is the
exact hole B3 exists to catch -> rc 3. Any inconclusive state (no creds, no turn:
URI, allocation failed, control not granted, non-403 error) -> rc 2, never 0.

TOPOLOGY: needs :443 (the WS join) and :3478 (TURN control) reachable — both are
up in standup's pre-firewall exposure phase (gate B1 already probes :3478/:5349
there). It does NOT need the relay range (50000-60000) open: the refusal is a
control-plane 403 before any relay port is used. So B3 stays in the exposure
gate; no ordering change.

The private sentinel set is MANDATORY and hard-coded (NOT env-overridable) — an
operator env value must never shrink a firewall-opening gate's tested set to zero.

Env: LK_URL (wss signaling), LK_API_KEY, LK_API_SECRET, LK_ROOM (optional),
     B3_EXTRA_PRIVATE_PEERS (optional, comma list — ADDS sentinels, cannot remove),
     B3_PUBLIC_CONTROL (optional; default 1.1.1.1),
     NODE_IP (optional; if set, the advertised TURN host must equal it).
Deps: websockets, livekit-api (livekit.protocol.rtc), aioice. No turnutils/coturn.
"""
import asyncio, os, sys, json, time, urllib.parse
import websockets
from livekit import api
from livekit.protocol.rtc import SignalResponse
from aioice import turn, stun

URL     = os.environ["LK_URL"]
KEY     = os.environ["LK_API_KEY"]
SECRET  = os.environ["LK_API_SECRET"]
ROOM    = os.environ.get("LK_ROOM", f"b3-probe-{int(time.time())}")
# SSRF-critical restricted ranges, one representative SENTINEL IP each — all REFUSED
# (403) on enspyr v1.13.5: RFC1918 (10/8, 172.16/12, 192.168/16), link-local + cloud
# metadata (169.254/16), loopback (127/8). This set is MANDATORY and HARD-CODED — it
# is NOT env-overridable, because a firewall-OPENING gate must never let an operator
# env value (an empty/typo'd override, a "disable for debug") shrink the tested set
# to zero and still certify OK (cage-match #129, Carnot+Tesla: empty B3_PRIVATE_PEERS
# was a fail-OPEN). B3_EXTRA_PRIVATE_PEERS may only ADD peers, never remove.
#   Sampling caveat: each entry is ONE sentinel per CIDR, so this proves the policy
#   for those exact destinations, not every address in each block (a regression that
#   denied 10.0.0.1 but allowed 10.99.99.99 would pass). It is a representative probe,
#   not an exhaustive range proof — the gate output and docs say "sentinel", not
#   "every private IP". NOT covered (tracked, task #6): 100.64/10 (RFC 6598 CGNAT,
#   found ALLOWED) and IPv6 forms (ULA fc00::/7, link-local fe80::/10, ::1, and
#   IPv4-mapped ::ffff:10.x) — excluded here so the gate doesn't false-block, not
#   because they're proven safe.
MANDATORY_PRIVATE_PEERS = [
    "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "127.0.0.1"]
EXTRA_PRIVATE_PEERS = [p.strip() for p in
                       os.environ.get("B3_EXTRA_PRIVATE_PEERS", "").split(",") if p.strip()]
PRIVATE_PEERS = MANDATORY_PRIVATE_PEERS + EXTRA_PRIVATE_PEERS
# Positive control: a PUBLIC peer whose permission MUST be granted (200). Without
# it, "403 for a private peer" could be a server that denies EVERY permission —
# the refusal would be vacuous. The control proves permissions work in general, so
# the private 403 is specifically the RFC1918/CIDR policy. It is re-checked AFTER
# the private matrix too (a mid-run global/quota 403 must not read as policy deny).
# (No packets are sent to it; only a TURN permission is created on a throwaway
# allocation, then torn down.)
PUBLIC_CONTROL = os.environ.get("B3_PUBLIC_CONTROL", "1.1.1.1")
# Optional pin: the TURN host the SFU advertises must equal the box the firewall
# will open (NODE_IP). Guards "probe the wrong coil, open the right port" (Tesla):
# a mis-advertised ice host would otherwise certify a DIFFERENT TURN than the one
# being exposed. Skipped when NODE_IP is unset (can't verify).
NODE_IP = os.environ.get("NODE_IP", "").strip()


def _block(reason: str) -> "int":
    """Fail-closed exit: print the structured line and return rc 2 (inconclusive)."""
    print("B3_ASSERT=" + json.dumps({"result": "BLOCK", "reason": reason}), flush=True)
    return 2


def mint() -> str:
    grants = api.VideoGrants(room_join=True, room=ROOM,
                             can_publish=False, can_subscribe=True)
    return (api.AccessToken(KEY, SECRET)
            .with_identity("b3-probe").with_name("b3-probe")
            .with_grants(grants).to_jwt())


async def extract_turn_creds(timeout: float = 15.0):
    """Speak the LiveKit signal WS directly; return the ice_servers from the first
    SignalResponse.join. The SFU emits JoinResponse immediately on a valid-token
    upgrade, before we send anything."""
    token = mint()
    signal = URL.rstrip("/") + "/rtc?" + urllib.parse.urlencode({
        "access_token": token, "auto_subscribe": "0",
        "protocol": "15", "sdk": "python", "version": "0.0.0",
    })
    async with websockets.connect(signal, max_size=None,
                                  open_timeout=timeout, close_timeout=5) as ws:
        deadline = time.monotonic() + timeout
        while True:
            # Bound each recv to the REMAINING budget, not a fresh full `timeout`
            # each iteration — else a server dribbling non-join frames could keep
            # this alive far past `timeout` (cage-match #129, Carnot). remaining<=0
            # ends it, fail-closed.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if isinstance(msg, str):
                # We requested protobuf; a text frame is JSON signaling we don't parse here.
                continue
            sr = SignalResponse.FromString(msg)
            if sr.WhichOneof("message") == "join":
                return list(sr.join.ice_servers)


def pick_udp_turns(ice_servers):
    """Return EVERY (host, port, username, credential) for the plain UDP TURN URIs the
    SFU advertised — not just the first (cage-match #129, Tesla: a benign first URI
    could mask a later open one; the gate must probe them all).

    We deliberately probe turn:...:3478?transport=udp — NOT turns:/5349. task #4:
    TLS/5349 relay is a known gap (externalTLS:false), so probing it would fail B3
    for the wrong reason. A STUN-only URI (stun:) carries no relay and is skipped."""
    out = []
    for s in ice_servers:
        user, cred = s.username, s.credential
        for uri in s.urls:
            low = uri.lower()
            if not low.startswith("turn:"):        # skip stun: and turns:
                continue
            if "transport=udp" not in low and "transport=" in low:
                continue                            # explicit non-udp transport
            body = uri.split(":", 1)[1].split("?", 1)[0]   # host[:port]
            if body.startswith("["):               # bracketed IPv6 literal: [::1]:3478
                host, _, rest = body[1:].partition("]")
                port = rest.lstrip(":") or "3478"
            elif ":" in body:
                host, port = body.rsplit(":", 1)
            else:
                host, port = body, "3478"
            out.append((host, int(port), user, cred))
    return out


async def create_permission(host, port, user, cred, peer):
    """Allocate with the session cred, then issue ONE CreatePermission for `peer`
    and classify the server's answer. Uses aioice's TURN client (mature, RFC-tested
    long-term-cred auth + 401/nonce re-challenge — the exact handling turnutils
    botched). Returns {allocate_ok, verdict, code} where verdict is one of:
      ALLOWED  (200) — server permitted a relay permission to this peer
      REFUSED  (403) — server forbade it (the desired policy)
      ERR<n>         — some other error code
      NO_ALLOC       — allocation itself failed (bad cred / unreachable)"""
    loop = asyncio.get_event_loop()
    server = (host, port)
    inner_transport, proto = await loop.create_datagram_endpoint(
        lambda: turn.TurnClientUdpProtocol(
            server, username=user, password=cred, lifetime=600, channel_refresh_time=500),
        remote_addr=server)
    try:
        try:
            relay = await asyncio.wait_for(proto.connect(), timeout=15)   # ALLOCATE = positive control
        except Exception as e:
            return {"peer": peer, "allocate_ok": False, "verdict": "NO_ALLOC",
                    "code": None, "detail": repr(e)}
        req = stun.Message(message_method=stun.Method.CREATE_PERMISSION,
                           message_class=stun.Class.REQUEST)
        req.attributes["XOR-PEER-ADDRESS"] = (peer, 9)   # port is immaterial to a permission
        try:
            await asyncio.wait_for(proto.request_with_retry(req), timeout=15)
            return {"peer": peer, "allocate_ok": True, "verdict": "ALLOWED",
                    "code": 200, "relay": list(relay)}
        except stun.TransactionFailed as e:
            code = e.response.attributes.get("ERROR-CODE", (None, ""))[0]
            return {"peer": peer, "allocate_ok": True,
                    "verdict": "REFUSED" if code == 403 else f"ERR{code}",
                    "code": code, "relay": list(relay)}
        except (asyncio.TimeoutError, stun.TransactionTimeout) as e:
            # Allocate succeeded but the permission got no answer — distinct from a
            # broken cred (NO_ALLOC). Inconclusive, not refused (cage-match #129, Carnot:
            # don't report "no allocation" when the allocation in fact succeeded).
            return {"peer": peer, "allocate_ok": True, "verdict": "PERM_TIMEOUT",
                    "code": None, "relay": list(relay), "detail": repr(e)}
    finally:
        # Reversible cleanup: cancel the allocation's refresh loop (aioice starts one
        # in connect()) and best-effort deallocate (Refresh lifetime=0) so repeated
        # runs don't accrete allocations on a public TURN surface (cage-match #129,
        # Carnot); then drop the socket.
        rt = getattr(proto, "refresh_task", None)
        if rt is not None:
            rt.cancel()
        try:
            await asyncio.wait_for(proto.delete(), timeout=5)
        except Exception:
            pass
        inner_transport.close()


async def main() -> int:
    try:
        ice = await extract_turn_creds()
    except Exception as e:
        return _block(f"signal-join extraction failed: {e!r}")
    if not ice:
        return _block("no ice_servers in the SFU JoinResponse — cannot extract a TURN cred")

    turns = pick_udp_turns(ice)
    if not turns:
        return _block("JoinResponse carried no usable turn:...:udp URI "
                      f"(saw {[list(s.urls) for s in ice]})")
    if not PRIVATE_PEERS:            # defensive: the mandatory set is hard-coded non-empty
        return _block("no private peers to test — the mandatory SSRF-critical set is empty (bug)")

    # Host pin: every advertised TURN host must be the box the firewall will open.
    if NODE_IP:
        mismatched = sorted({h for (h, _, _, _) in turns if h != NODE_IP})
        if mismatched:
            return _block(f"advertised TURN host(s) {mismatched} != NODE_IP {NODE_IP} — the SFU is "
                          "steering clients to a TURN other than the box being exposed; refusing to certify")

    async def probe(host, port, user, cred, peer):
        try:
            res = await create_permission(host, port, user, cred, peer)
        except Exception as e:
            res = {"peer": peer, "allocate_ok": None, "verdict": "PROBE_ERR", "detail": repr(e)}
        print("  probe " + json.dumps(res), flush=True)
        return res

    # EVERY advertised turn:udp endpoint must pass the full matrix — a single open one
    # is the SSRF hole regardless of how many others deny (cage-match #129, Tesla).
    all_results = []
    for (host, port, user, cred) in turns:
        print(f"  endpoint turn:{host}:{port} user={user[:8]}… "
              f"(from SFU JoinResponse, {len(ice)} ice_server(s), {len(turns)} udp URI(s))", flush=True)

        # Positive control FIRST: a public peer's permission must be GRANTED (200). If
        # not, the server denies permissions broadly and a private 403 proves nothing
        # about an RFC1918-specific policy — fail closed.
        control = await probe(host, port, user, cred, PUBLIC_CONTROL)
        if not control.get("allocate_ok"):
            return _block(f"no TURN allocation succeeded on turn:{host}:{port} — credential/"
                          "reachability problem, cannot certify relay-deny")
        if control["verdict"] != "ALLOWED":
            return _block(f"public-peer control {PUBLIC_CONTROL} not permitted on turn:{host}:{port} "
                          f"(got {control['verdict']}/{control.get('code')}) — server denies "
                          "permissions broadly; cannot isolate an RFC1918-specific deny")

        results = [await probe(host, port, user, cred, p) for p in PRIVATE_PEERS]
        allowed = [r["peer"] for r in results if r["verdict"] == "ALLOWED"]
        inconclusive = [r["peer"] for r in results if r["verdict"] not in ("ALLOWED", "REFUSED")]
        if allowed:
            print("B3_ASSERT=" + json.dumps({"result": "FAIL",
                  "reason": "server ALLOWED a relay permission to a private sentinel",
                  "endpoint": f"turn:{host}:{port}", "allowed": allowed, "peers": results}), flush=True)
            return 3
        if inconclusive:
            return _block(f"non-403 result creating permission on turn:{host}:{port} for "
                          f"{inconclusive} — cannot positively confirm refusal (fail-closed): {results}")

        # Re-check the control AFTER the private matrix: it must STILL be ALLOWED. If a
        # mid-run global/quota/rate 403 kicked in, the private "403"s were not policy
        # refusals — the OK would be false (cage-match #129, Tesla). This proves the
        # server stayed selectively permissive across the whole matrix.
        control2 = await probe(host, port, user, cred, PUBLIC_CONTROL)
        if control2["verdict"] != "ALLOWED":
            return _block(f"public control stopped being permitted after the private matrix on "
                          f"turn:{host}:{port} (got {control2['verdict']}/{control2.get('code')}) — a "
                          "global/quota 403 may be masquerading as policy refusal; cannot certify")
        all_results.append({"endpoint": f"turn:{host}:{port}", "peers": results})

    print("B3_ASSERT=" + json.dumps({"result": "OK",
          "reason": f"every advertised turn:udp endpoint ({len(turns)}): allocation succeeded, public "
                    "control allowed before+after, every SSRF-critical private sentinel refused (403)",
          "sentinels": PRIVATE_PEERS, "endpoints": all_results}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
