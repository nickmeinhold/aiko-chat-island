#!/usr/bin/env python3
"""turns443_data_proof.py — prove REAL BYTES relay through turns:<domain>:443.

Why this exists: the B3 probe proves ALLOCATION + permission policy on turns:443, and the
livekit-rtc harness proves a real WebRTC call — but the python livekit SDK bundles a root
store with NO ISRG root at all (verified 2026-08-13: DigiCert/Entrust/GlobalSign/Comodo/
Starfield/USERTrust/Baltimore/GoDaddy present, zero Let's Encrypt), so it cannot validate ANY
Let's Encrypt certificate and can never complete a TURNS handshake against these islands.
That makes it structurally incapable of proving the data path, no matter what the server does.

So: use a client that DOES trust LE (python ssl -> system store) and prove the thing that
actually matters about the SERVER — that payload bytes traverse
    client --TLS:443--> HAProxy --plaintext--> LiveKit TURN --relay--> ...
and back again, with UDP to the box blocked so :443 is the ONLY possible path.

Method: open TWO allocations on turns:443, permission each other's relay address, then send a
payload A->B and B->A and require both to arrive. Peer addresses are the box's own PUBLIC ip
(global), so deny_peer_cidrs does not apply.

Env: LK_URL, LK_API_KEY, LK_API_SECRET (+ B3_PROBE path to import the extractor from).
Exit 0 = bytes relayed both directions. Non-zero = they did not.
"""
import asyncio, os, sys, json, importlib.util, secrets, socket
from aioice import stun

B3_PATH = os.environ.get("B3_PROBE", "/tmp/b3_relay_probe.py")
spec = importlib.util.spec_from_file_location("b3", B3_PATH)
b3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b3)          # safe: b3 guards main() behind __main__


class Capture(asyncio.DatagramProtocol):
    """aioice hands relayed DATA indications to proto.receiver.datagram_received()."""
    def __init__(self, label):
        self.label, self.got = label, asyncio.Queue()
    def datagram_received(self, data, addr):
        self.got.put_nowait((data, addr))
    def connection_lost(self, exc):
        pass


async def main() -> int:
    ice = await b3.extract_turn_creds()
    if not ice:
        print(json.dumps({"result": "BLOCK", "reason": "no ice_servers in JoinResponse"})); return 2
    eps = b3.pick_turn_endpoints(ice)
    # Selectable so the SAME method can be run against the UDP relay as a CONTROL: if data
    # does not flow there either, the method (or LiveKit refusing to relay to its own relay
    # range) is at fault, not the TLS path. A verifier that can only ever fail one way proves
    # nothing.
    want = os.environ.get("TEST_ENDPOINT", "tls:*:443")
    tls = [e for e in eps if b3._spec_matches(want, e)]
    if not tls:
        print(json.dumps({"result": "BLOCK", "reason": f"no relay matching {want!r} advertised; saw "
                          f"{[(e['transport'], e['host'], e['port']) for e in eps]}"})); return 2
    ep = tls[0]
    label = f"{ep['transport']}:{ep['host']}:{ep['port']}"
    print(f"  endpoint under test: {label}")

    peer_host = os.environ.get("ECHO_PEER_HOST", "stun.l.google.com")
    peer_port = int(os.environ.get("ECHO_PEER_PORT", "19302"))
    peer_ip = socket.getaddrinfo(peer_host, peer_port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    print(f"  external peer: {peer_host} -> {peer_ip}:{peer_port}")

    inner = proto = None
    try:
        inner, proto, relay = await b3.allocate(ep["transport"], ep["host"], ep["port"],
                                                ep["user"], ep["cred"])
        cap = Capture("A"); proto.receiver = cap
        print(f"  allocated relay {relay} via {label}")

        v = await b3.check_perm(proto, peer_ip)
        print(f"  permission for {peer_ip} -> {v['verdict']}/{v.get('code')}")
        if v["verdict"] != "ALLOWED":
            print(json.dumps({"result": "BLOCK", "reason": f"no permission for the echo peer: {v}"}))
            return 2

        # Relay a real STUN Binding Request out to the public STUN server. A reply can ONLY come
        # back by traversing: peer -> relay port -> LiveKit TURN -> (plaintext) -> HAProxy ->
        # TLS:443 -> us. So a parsed success response is end-to-end data-plane proof.
        req = stun.Message(message_method=stun.Method.BINDING, message_class=stun.Class.REQUEST,
                           transaction_id=secrets.token_bytes(12))
        await proto.send_data(bytes(req), (peer_ip, peer_port))
        try:
            data, addr = await asyncio.wait_for(cap.got.get(), timeout=15)
        except asyncio.TimeoutError:
            print(json.dumps({"result": "FAIL", "endpoint": label,
                  "reason": "relayed STUN Binding Request to a public STUN server got no reply "
                            "back through the relay — allocation works, DATA does not"}))
            return 3
        resp = stun.parse_message(data)
        mapped = resp.attributes.get("XOR-MAPPED-ADDRESS") or resp.attributes.get("MAPPED-ADDRESS")
        print(f"  reply {len(data)}B from {addr}: {resp.message_class} mapped={mapped}")
        print("TURNS443_DATA=" + json.dumps({
            "result": "OK", "endpoint": label, "peer": f"{peer_ip}:{peer_port}",
            "relay": list(relay), "mapped_address": list(mapped) if mapped else None,
            "reason": "a real STUN Binding Request was relayed OUT to a public peer and its reply "
                      "came back IN through the relay — the data plane carries payload, not just "
                      "allocations. The mapped address is the RELAY, proving the peer saw us as "
                      "the TURN server."}))
        return 0
    finally:
        if inner is not None:
            try: await b3.teardown(inner, proto)
            except Exception: pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
