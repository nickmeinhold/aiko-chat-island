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
     B3_PUBLIC_CONTROL (optional; default 1.1.1.1; must be a global IP),
     NODE_IP + B3_EXPECT_HOST (optional; advertised TURN host must resolve into their
     IP set — gate_B3 passes B3_EXPECT_HOST=TURN_DOMAIN so the exposure path always pins),
     B3_REQUIRE_ENDPOINT (optional, comma list of '<transport>[:<host>][:<port>]' with '*'
     wildcards; each MUST be live+passing or the run BLOCKs — the LIVENESS assertion, see below).
Deps: websockets, livekit-api (livekit.protocol.rtc), aioice. No turnutils/coturn (the
      PROBE has no coturn dep; gate B1 in e2e_media_relay.py still uses turnutils_uclient).
"""
import asyncio, os, sys, json, time, ssl, socket, ipaddress, urllib.parse
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
#   "every private IP".
#   IPv6 (cage-match #129 r2, empirically resolved on enspyr v1.13.5, NOT a residual):
#   a native-IPv6 peer on this IPv4 allocation returns 443 Peer-Address-Family-Mismatch
#   (RFC 6156) — the server cannot relay IPv4-allocation traffic to an IPv6 peer, and the
#   relay address is IPv4-only, so IPv6 private targets are not a vector here. IPv4-mapped
#   forms (::ffff:10.0.0.1) are REFUSED 403 — the deny applies, no bypass. So IPv6 forms
#   are deliberately NOT in the mandatory set: they'd 443 (inconclusive) and false-BLOCK a
#   server that is not actually reachable over IPv6. task #6 tracks the IPv6-ALLOCATION
#   question (would the server grant an IPv6 relay if one were requested).
MANDATORY_PRIVATE_PEERS = [
    "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "127.0.0.1"]
EXTRA_PRIVATE_PEERS = [p.strip() for p in
                       os.environ.get("B3_EXTRA_PRIVATE_PEERS", "").split(",") if p.strip()]
PRIVATE_PEERS = MANDATORY_PRIVATE_PEERS + EXTRA_PRIVATE_PEERS
# INFORMATIONAL sentinel — a band LiveKit's default deny does NOT cover: 100.64/10
# (RFC 6598 CGNAT), found ALLOWED (200). It is NOT mandatory (that would BLOCK every
# standup until the server denies it, and its real reachability/severity on OCI is
# still open — task #6). But a firewall-opening gate must not emit a clean OK that
# HIDES a known-open private-ish band (cage-match #129 r2, Tesla): so we probe it and,
# if still ALLOWED, NAME it in the OK verdict as a surfaced known-gap.
CGNAT_INFO_SENTINEL = "100.64.0.1"
# Positive control: a PUBLIC peer whose permission MUST be granted (200) — otherwise a
# private 403 is vacuous (a server denying EVERY permission). Re-checked AFTER the
# private matrix on the SAME allocation too (a mid-run global/quota 403 must not read
# as policy deny). It must itself be a GLOBAL address (cage-match #129 r2, Tesla: a
# private/empty B3_PUBLIC_CONTROL would silently degrade the control) — validated below.
PUBLIC_CONTROL = os.environ.get("B3_PUBLIC_CONTROL", "1.1.1.1").strip()
# Host pin: the TURN host the SFU advertises must resolve to the SAME box the firewall
# will open — else the gate certifies one TURN and opens another ("probe the wrong coil,
# open the right port", Tesla). Compared by RESOLVED IP SET, not raw string, so an FQDN
# advertisement (turn.<domain>) and a dotted NODE_IP are reconciled (cage-match #129 r2).
# Expected identity comes from NODE_IP and/or B3_EXPECT_HOST (gate_B3 passes the exposure
# TURN_DOMAIN). If neither is set (off-box manual run) the pin can't be verified and the
# probe WARNs; standup always passes B3_EXPECT_HOST, so the exposure path always pins.
NODE_IP = os.environ.get("NODE_IP", "").strip()
EXPECT_HOST = os.environ.get("B3_EXPECT_HOST", "").strip()
# LIVENESS assertion (task #6 finding F1). This probe answers a SECURITY question — "does the
# running relay refuse RFC1918" — and for that, an advertised-but-unallocatable endpoint is
# correctly non-blocking: you cannot relay through a relay you cannot allocate on. But the
# turn-443 RUNBOOK also designates this probe as the post-cutover ACCEPTANCE GATE, worded
# "turns:443 must flip UNREACHABLE -> ALLOCATED" — and nothing here ASSERTED that. The task #6
# rehearsal caught it empirically: a run with tls:turn.<domain>:443 UNREACHABLE still returned
# result:OK / exit 0 on the strength of the UDP endpoint alone. So a cutover that left TURN-over-
# TLS dead — the exact failure the cutover exists to fix — would be green-lit by its own gate.
# Sound as a security assertion, fail-OPEN as a liveness one; this closes the liveness half
# WITHOUT touching the security semantics. Opt-in, mirroring the NODE_IP/B3_EXPECT_HOST
# warn-vs-pin pattern, so an off-box manual run still works unpinned.
REQUIRE_ENDPOINTS = [s.strip() for s in os.environ.get("B3_REQUIRE_ENDPOINT", "").split(",") if s.strip()]


def _spec_matches(spec, ep):
    """'<transport>[:<host>][:<port>]', '*' wildcards a field. Matched against the endpoint's
    PARSED fields, not its label string, so an IPv6 host (which contains colons) can't be
    mis-split. 'tls:443' (no host) is accepted as transport+port."""
    transport, _, rest = spec.partition(":")
    if transport not in ("", "*") and transport.lower() != str(ep["transport"]).lower():
        return False
    if not rest:
        return True
    host, sep, port = rest.rpartition(":")
    if not sep:                      # no second colon -> the remainder is a bare port
        host, port = "", rest
    if host not in ("", "*") and host.lower() != str(ep["host"]).lower():
        return False
    if port not in ("", "*") and str(port) != str(ep["port"]):
        return False
    return True


def _resolve_ips(name: str) -> set:
    """Resolve a host (IP literal or FQDN) to its set of IP strings; empty on failure."""
    if not name:
        return set()
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(name, None)}
    except (socket.gaierror, OSError):
        return set()


def _is_global(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_global
    except ValueError:
        return False


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


def pick_turn_endpoints(ice_servers):
    """Return EVERY advertised RELAY endpoint as a dict {transport, host, port, user, cred,
    uri} — across ALL transports, not just UDP (cage-match #129 r3: enspyr advertises BOTH
    a turn:udp AND a turns:tcp relay; probing only UDP left the TLS relay's deny untested).
    Each is tested on its own transport; probe them all (a benign endpoint can't vouch for
    another). stun: URIs carry no relay and are skipped.

    transport ∈ {udp, tcp, tls}: turns:→tls; turn:+transport=tcp→tcp; turn:+transport=udp
    OR no-transport-on-3478→udp. A no-transport turn:host:<non-3478> is ambiguous → tcp
    (conservative: it will be allocate-tested, not silently datagram-probed)."""
    out = []
    for s in ice_servers:
        user, cred = s.username, s.credential
        for uri in s.urls:
            low = uri.lower()
            if low.startswith("stun:"):
                continue
            if low.startswith("turns:"):
                scheme, rest = "tls", uri[len("turns:"):]
            elif low.startswith("turn:"):
                scheme, rest = "turn", uri[len("turn:"):]
            else:
                continue
            body = rest.split("?", 1)[0]                    # host[:port]
            if body.startswith("["):                        # [IPv6]:port
                host, _, tail = body[1:].partition("]")
                port = tail.lstrip(":")
            elif ":" in body:
                host, port = body.rsplit(":", 1)
            else:
                host, port = body, ""
            if scheme == "tls":
                transport, port = "tls", int(port or 5349)
            elif "transport=udp" in low:
                transport, port = "udp", int(port or 3478)
            elif "transport=tcp" in low:
                transport, port = "tcp", int(port or 3478)
            elif not port or port == "3478":                # no transport, default port → udp
                transport, port = "udp", 3478
            else:                                            # no transport, non-default port → treat as tcp
                transport, port = "tcp", int(port)
            out.append({"transport": transport, "host": host, "port": port,
                        "user": user, "cred": cred, "uri": uri})
    return out


async def teardown(inner_transport, proto):
    """Reversible cleanup: cancel the refresh loop aioice starts in connect(), best-effort
    deallocate (Refresh lifetime=0) so repeated runs don't accrete allocations on a public
    TURN surface (cage-match #129 r1, Carnot), then drop the socket."""
    rt = getattr(proto, "refresh_task", None)
    if rt is not None:
        rt.cancel()
    try:
        await asyncio.wait_for(proto.delete(), timeout=5)
    except Exception:
        pass
    inner_transport.close()


async def allocate(transport, host, port, user, cred):
    """Create ONE TURN allocation with the session cred over the endpoint's transport
    (udp datagram, or tcp/tls stream). Returns (inner_transport, proto, relay); raises on
    failure (socket cleaned up). The caller issues MANY CreatePermissions on this single
    allocation — the attacker's real shape, and the only shape under which the post-matrix
    control re-check is meaningful (cage-match #129 r2/r3, Tesla)."""
    loop = asyncio.get_event_loop()
    server = (host, port)
    if transport == "udp":
        inner_transport, proto = await loop.create_datagram_endpoint(
            lambda: turn.TurnClientUdpProtocol(
                server, username=user, password=cred, lifetime=600, channel_refresh_time=500),
            remote_addr=server)
    else:  # tcp or tls
        sslctx = ssl.create_default_context() if transport == "tls" else None
        inner_transport, proto = await loop.create_connection(
            lambda: turn.TurnClientTcpProtocol(
                server, username=user, password=cred, lifetime=600, channel_refresh_time=500),
            host=host, port=port, ssl=sslctx)
    try:
        relay = await asyncio.wait_for(proto.connect(), timeout=15)
    except Exception:
        await teardown(inner_transport, proto)
        raise
    return inner_transport, proto, relay


async def check_perm(proto, peer):
    """Issue ONE CreatePermission for `peer` on an EXISTING allocation; classify the
    control-plane answer: ALLOWED(200) / REFUSED(403) / ERR<code> / PERM_TIMEOUT."""
    req = stun.Message(message_method=stun.Method.CREATE_PERMISSION,
                       message_class=stun.Class.REQUEST)
    req.attributes["XOR-PEER-ADDRESS"] = (peer, 9)   # port is immaterial to a permission
    try:
        await asyncio.wait_for(proto.request_with_retry(req), timeout=15)
        res = {"peer": peer, "verdict": "ALLOWED", "code": 200}
    except stun.TransactionFailed as e:
        code = e.response.attributes.get("ERROR-CODE", (None, ""))[0]
        res = {"peer": peer, "verdict": "REFUSED" if code == 403 else f"ERR{code}", "code": code}
    except (asyncio.TimeoutError, stun.TransactionTimeout) as e:
        res = {"peer": peer, "verdict": "PERM_TIMEOUT", "code": None, "detail": repr(e)}
    print("  probe " + json.dumps(res), flush=True)
    return res


async def main() -> int:
    # The positive control must itself be a GLOBAL address — a private/empty
    # B3_PUBLIC_CONTROL would make the anti-vacuity check meaningless (cage-match #129 r2).
    if not _is_global(PUBLIC_CONTROL):
        return _block(f"B3_PUBLIC_CONTROL {PUBLIC_CONTROL!r} is not a global/public IP — the positive "
                      "control would be meaningless; set a public literal (default 1.1.1.1)")

    try:
        ice = await extract_turn_creds()
    except Exception as e:
        return _block(f"signal-join extraction failed: {e!r}")
    if not ice:
        return _block("no ice_servers in the SFU JoinResponse (or timed out waiting for a join "
                      "frame) — cannot extract a TURN cred")

    endpoints = pick_turn_endpoints(ice)
    if not endpoints:
        return _block("JoinResponse carried no usable turn:/turns: relay URI "
                      f"(saw {[list(s.urls) for s in ice]})")
    if not PRIVATE_PEERS:            # defensive: the mandatory set is hard-coded non-empty
        return _block("no private peers to test — the mandatory SSRF-critical set is empty (bug)")

    # Host pin (resolve-and-compare, not raw string): every advertised TURN host must
    # resolve into the exposed box's identity (NODE_IP and/or B3_EXPECT_HOST). No pin set
    # (off-box manual run) → WARN and continue; standup always passes B3_EXPECT_HOST, so
    # the exposure path always pins (cage-match #129 r2, Carnot+Tesla).
    expected = _resolve_ips(NODE_IP) | _resolve_ips(EXPECT_HOST)
    if expected:
        for e in endpoints:
            adv = _resolve_ips(e["host"]) or {e["host"]}
            if not (adv & expected):
                return _block(f"advertised TURN host {e['host']} (→{sorted(adv)}) does not resolve into "
                              f"the exposed box {sorted(expected)} (NODE_IP/B3_EXPECT_HOST) — probe-the-"
                              "wrong-coil-open-the-right-port; refusing to certify")
    else:
        print("  WARN B3: no NODE_IP / B3_EXPECT_HOST set — cannot pin the advertised TURN to the box "
              "being exposed (off-box manual run only; standup passes B3_EXPECT_HOST).", flush=True)

    # Test EVERY advertised relay endpoint on its OWN transport, each on a SINGLE allocation
    # (attacker's real shape; the after-control re-check is only meaningful on the same
    # allocation). A LIVE endpoint MUST pass control-before + sentinels-refused + control-
    # after. An UNREACHABLE endpoint (allocate fails/times out — e.g. an advertised-but-dead
    # turns: relay) is NOT an SSRF vector (you can't relay through a relay you can't allocate
    # on) → surfaced, non-blocking. At least one endpoint must be LIVE and pass, else there
    # is no reachable relay to certify → BLOCK (cage-match #129 r3).
    tested, unreachable, tested_eps = [], [], []
    for ep in endpoints:
        label = f"{ep['transport']}:{ep['host']}:{ep['port']}"
        print(f"  endpoint {label} user={ep['user'][:8]}… "
              f"({len(ice)} ice_server(s), {len(endpoints)} relay URI(s))", flush=True)
        try:
            inner, proto, relay = await allocate(ep["transport"], ep["host"], ep["port"],
                                                 ep["user"], ep["cred"])
        except (asyncio.TimeoutError, OSError, ssl.SSLError, ConnectionError) as e:
            # A genuine CONNECTIVITY failure (connect timeout / refused / TLS handshake / no
            # TURN response) — the endpoint can't be allocated on, so it is not a relay vector
            # (you can't relay through what you can't allocate). Surfaced, non-blocking. NOTE:
            # only connectivity errors land here — a code bug or unexpected error propagates and
            # crashes the probe (rc≠0 → gate BLOCKs), so a bug can never masquerade as
            # "unreachable" and silently pass an untested relay (cage-match #129 r3, caught a
            # NameError doing exactly that).
            print(f"  UNREACHABLE {label}: allocate failed ({e!r}) — not a relay vector, surfaced", flush=True)
            unreachable.append({"endpoint": label, "uri": ep["uri"], "detail": repr(e)})
            continue
        try:
            print(f"  allocated relay {list(relay)}", flush=True)

            control = await check_perm(proto, PUBLIC_CONTROL)     # must be GRANTED (200)
            if control["verdict"] != "ALLOWED":
                return _block(f"public-peer control {PUBLIC_CONTROL} not permitted on {label} "
                              f"(got {control['verdict']}/{control.get('code')}) — server denies "
                              "permissions broadly; cannot isolate an RFC1918-specific deny")

            results = [await check_perm(proto, p) for p in PRIVATE_PEERS]
            allowed = [r["peer"] for r in results if r["verdict"] == "ALLOWED"]
            inconclusive = [r["peer"] for r in results if r["verdict"] not in ("ALLOWED", "REFUSED")]
            if allowed:
                print("B3_ASSERT=" + json.dumps({"result": "FAIL",
                      "reason": "server ALLOWED a relay permission to a private sentinel",
                      "endpoint": label, "allowed": allowed, "peers": results}), flush=True)
                return 3
            if inconclusive:
                return _block(f"non-403 result creating permission on {label} for {inconclusive} — "
                              f"cannot positively confirm refusal (fail-closed): {results}")

            # Re-check control on the SAME allocation AFTER the matrix — still ALLOWED, else a
            # mid-run global/quota 403 was masquerading as policy refusal (cage-match #129 r2).
            control2 = await check_perm(proto, PUBLIC_CONTROL)
            if control2["verdict"] != "ALLOWED":
                return _block(f"public control stopped being permitted after the private matrix on "
                              f"{label} (got {control2['verdict']}/{control2.get('code')}) — a global/"
                              "quota 403 may be masquerading as policy refusal; cannot certify")

            # Informational: probe a KNOWN-open private-ish band (100.64/10 CGNAT) so a clean OK
            # cannot HIDE it — surfaced in the verdict, not buried in a comment (cage-match #129
            # r2, Tesla). Non-blocking (task #6 owns the severity call).
            cgnat = await check_perm(proto, CGNAT_INFO_SENTINEL)
            tested.append({"endpoint": label, "peers": results, "cgnat_100_64_0_1": cgnat["verdict"]})
            tested_eps.append(ep)
        finally:
            await teardown(inner, proto)

    # The required endpoints must be LIVE AND PASSING — checked against `tested`, so an endpoint
    # that landed in `unreachable` (or was never advertised at all) fails this. Runs BEFORE the
    # no-tested BLOCK is irrelevant: both are fail-closed, but this one names WHICH endpoint the
    # caller was relying on, which is the whole point of a gate.
    if REQUIRE_ENDPOINTS:
        missing = [spec for spec in REQUIRE_ENDPOINTS
                   if not any(_spec_matches(spec, ep) for ep in tested_eps)]
        if missing:
            return _block(
                f"required relay endpoint(s) {missing} were not proven live: they are absent from "
                f"the tested set {[t['endpoint'] for t in tested]}"
                + (f" (unreachable: {[u['endpoint'] for u in unreachable]})" if unreachable else "")
                + " — the caller pinned these as the endpoints that MUST work (B3_REQUIRE_ENDPOINT), "
                  "so a pass here would certify a relay path that does not exist")

    if not tested:
        return _block(f"no advertised relay endpoint was reachable to certify (all unreachable: "
                      f"{[u['endpoint'] for u in unreachable]}) — cannot prove a relay-deny on a relay "
                      "nobody can allocate; refusing to open the range")

    known_open = sorted({t["endpoint"] for t in tested if t["cgnat_100_64_0_1"] == "ALLOWED"})
    verdict = {"result": "OK",
               "reason": f"{len(tested)} live relay endpoint(s) tested: allocation succeeded, public "
                         "control allowed before+after (same allocation), every SSRF-critical private "
                         "sentinel refused via control-plane CreatePermission (403). Sampled sentinels, "
                         "not a full-range proof.",
               "sentinels": PRIVATE_PEERS, "tested": tested}
    if unreachable:
        verdict["unreachable"] = unreachable   # advertised but un-allocatable (not a vector), surfaced
    if known_open:
        verdict["known_open_band"] = (f"100.64/10 (RFC 6598 CGNAT) still ALLOWED on {known_open} — "
                                      "SURFACED known gap (task #6), non-blocking by policy")
    print("B3_ASSERT=" + json.dumps(verdict), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
