"""Phase 1 WSS-slice verification (run against a live local gateway).

Proves: authed handshake, subscribe, authed send -> ack, live fanout to a second
client, ECHO SUPPRESSION (recipient gets exactly one frame, not the bus echo
too), and external-actor ingest -> fanout with sender_kind=actor.
"""
import asyncio, json, time, urllib.request, urllib.error
import websockets

BASE = "http://127.0.0.1:8095"
WS = "ws://127.0.0.1:8095/v1/ws"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


async def collect(ws, seconds):
    """Collect all frames arriving within `seconds`."""
    out = []
    try:
        while True:
            out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=seconds)))
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        pass
    return out


def reg(name):
    return call("POST", "/v1/auth/register",
                {"username": name, "display_name": name.title(), "password": "pw"})[1]


async def main():
    t = int(time.time())
    alice = reg(f"alice{t}")
    bob = reg(f"bob{t}")
    cid = next(c["id"] for c in call("GET", "/v1/channels")[1]["channels"]
               if c["aiko_channel"] == "general")

    results = []

    def ok(label, cond, detail=""):
        results.append(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    async with websockets.connect(f"{WS}?token={alice['access_token']}") as aws, \
               websockets.connect(f"{WS}?token={bob['access_token']}") as bws:
        await aws.send(json.dumps({"type": "subscribe", "channel_ids": [cid]}))
        await bws.send(json.dumps({"type": "subscribe", "channel_ids": [cid]}))
        await asyncio.sleep(0.3)

        body = f"hello-ws-{t}"
        cmid = f"cmid-{t}"
        await aws.send(json.dumps({"type": "send", "client_msg_id": cmid,
                                   "channel_id": cid, "body": body}))

        aframes, bframes = await asyncio.gather(collect(aws, 3.0), collect(bws, 3.0))

        acks = [f for f in aframes if f.get("type") == "ack" and f.get("client_msg_id") == cmid]
        ok("sender receives ack with server msg_id", len(acks) == 1 and bool(acks[0].get("msg_id")),
           f"acks={len(acks)}")
        server_msg_id = acks[0]["msg_id"] if acks else None

        b_msgs = [f for f in bframes if f.get("type") == "message" and f["msg"]["body"] == body]
        ok("recipient receives the message live", len(b_msgs) == 1, f"count={len(b_msgs)}")
        ok("ECHO SUPPRESSED: recipient gets exactly ONE (not the bus echo too)",
           len(b_msgs) == 1, f"count={len(b_msgs)}")
        if b_msgs:
            ok("message sender_kind=human", b_msgs[0]["msg"]["sender"]["kind"] == "human")
            ok("fanned msg_id == ack msg_id (client can dedupe vs optimistic)",
               b_msgs[0]["msg"]["msg_id"] == server_msg_id)

        # Persistence: exactly one row for this body.
        hist = call("GET", f"/v1/channels/{cid}/messages?limit=200")[1]["messages"]
        ok("persisted exactly once (no echo double-write)",
           sum(1 for m in hist if m["body"] == body) == 1)

        # External actor ingest (simulated REPL/LLM via the debug publish).
        ext_body = f"ext-{t}"
        call("POST", f"/v1/_debug/send?channel=general&username=some_repl&message={ext_body}")
        bframes2 = await collect(bws, 3.0)
        ext = [f for f in bframes2 if f.get("type") == "message" and f["msg"]["body"] == ext_body]
        ok("external-actor message fans out live", len(ext) == 1, f"count={len(ext)}")
        if ext:
            ok("external sender_kind=actor", ext[0]["msg"]["sender"]["kind"] == "actor")

    print(f"\n{'ALL PASS' if all(results) else 'SOME FAILED'} ({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
