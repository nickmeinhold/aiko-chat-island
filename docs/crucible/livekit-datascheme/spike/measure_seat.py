#!/usr/bin/env python3
"""G-ROUND-3 — how long does an ABANDONED Room keep its SFU participant seat?

Round 3 struck D1 on this, 4/4: `disconnect()` is raced against a deadline, the await is
abandoned, and the design then *declares the Room dead*. Threads and fds are flat — but
that is a statement about this process, not about the SFU. Tesla: "Death was not observed.
Death was announced."

The number D1 needs is the one nobody looked up: after we abandon, **how long does the
server keep the seat**, and does it ever leave at all? If the abandoned Room's native
threads keep answering keepalives, the answer is *never*, and D1's fail-closed restart
policy denies the capability forever rather than for a bounded window.

Three arms, because a single arm cannot distinguish "the ghost persists" from "my poller
cannot see departures":

  CONTROL   clean connect + clean disconnect. The seat MUST disappear. This is the
            positive control — without it a "ghost persists" reading is unfalsifiable.
  KILLED    the client process is SIGKILLed. The OS closes the socket, so this measures
            LiveKit's handling of an abrupt-but-signalled departure.
  ABANDONED D1's actual scenario: black-hole the SFU, wait_for(disconnect(), T) times out,
            abandon the await, un-black-hole, then watch the seat.

Caveat stated up front: `docker pause` freezes the SERVER's clock too, so any timeout that
would have elapsed during the pause is deferred. Elapsed time is therefore measured from
UNPAUSE, which is the honest reading of "how long after the network heals".

Env: LK_URL, LK_API_KEY, LK_API_SECRET
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

from livekit import api, rtc

URL = os.environ["LK_URL"]
KEY = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]
HTTP_URL = URL.replace("ws://", "http://").replace("wss://", "https://")


def mint(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (api.AccessToken(KEY, SECRET).with_identity(identity)
            .with_name(identity).with_grants(grants).to_jwt())


async def seats(room: str) -> list:
    """Who does the SERVER think is in this room? The only authority on the seat."""
    lk = api.LiveKitAPI(HTTP_URL, KEY, SECRET)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        return [(p.identity, api.ParticipantInfo.State.Name(p.state)) for p in res.participants]
    except Exception as exc:
        return [("<error>", repr(exc))]
    finally:
        await lk.aclose()


async def watch_seat(room: str, identity: str, budget_s: float, label: str) -> dict:
    """Poll until the identity leaves the server's roster, or the budget expires."""
    t0 = time.perf_counter()
    last = None
    while time.perf_counter() - t0 < budget_s:
        roster = await seats(room)
        present = [s for (i, s) in roster if i == identity]
        if present != last:
            print(f"    [{label}] t+{time.perf_counter()-t0:6.1f}s  {identity}: "
                  f"{present or 'GONE'}  (roster={len(roster)})", flush=True)
            last = present
        if not present:
            return {"left": True, "after_s": round(time.perf_counter() - t0, 1)}
        await asyncio.sleep(2.0)
    return {"left": False, "after_s": None, "budget_s": budget_s,
            "final_state": last}


def docker(action: str, container: str) -> None:
    subprocess.run(["docker", action, container], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def arm_control(args) -> dict:
    """A clean disconnect MUST free the seat. If this fails, every other arm is void."""
    room_name = f"seat-control-{int(time.time())}"
    ident = "control-participant"
    room = rtc.Room()
    await room.connect(URL, mint(room_name, ident))
    await asyncio.sleep(3)
    before = await seats(room_name)
    print(f"    [control] seated: {before}", flush=True)
    await room.disconnect()
    result = await watch_seat(room_name, ident, args.budget, "control")
    return {"arm": "CONTROL_clean_disconnect", "seated_before": len(before), **result}


async def arm_killed(args) -> dict:
    """Client process SIGKILLed: the OS closes the socket on our behalf."""
    room_name = f"seat-killed-{int(time.time())}"
    ident = "killed-participant"
    child = subprocess.Popen(
        [sys.executable, "-c", f'''
import asyncio, os, sys
sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})
from livekit import rtc, api
async def main():
    room = rtc.Room()
    grants = api.VideoGrants(room_join=True, room={room_name!r}, can_publish=True, can_subscribe=True)
    tok = (api.AccessToken({KEY!r}, {SECRET!r}).with_identity({ident!r})
           .with_name({ident!r}).with_grants(grants).to_jwt())
    await room.connect({URL!r}, tok)
    print("JOINED", flush=True)
    await asyncio.sleep(600)
asyncio.run(main())
'''], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        line = child.stdout.readline()
        if not line or "JOINED" in line:
            break
    await asyncio.sleep(3)
    before = await seats(room_name)
    print(f"    [killed] seated: {before}", flush=True)
    child.kill()
    child.wait()
    try:
        child.stdout.close()
    except Exception:
        pass
    result = await watch_seat(room_name, ident, args.budget, "killed")
    return {"arm": "KILLED_process_sigkill", "seated_before": len(before), **result}


async def arm_abandoned(args) -> dict:
    """D1's scenario, exactly: hung disconnect, await abandoned, Room 'declared dead'."""
    room_name = f"seat-abandoned-{int(time.time())}"
    ident = "abandoned-participant"
    room = rtc.Room()
    await room.connect(URL, mint(room_name, ident))
    await asyncio.sleep(3)
    before = await seats(room_name)
    print(f"    [abandoned] seated: {before}", flush=True)

    docker("pause", args.container)          # the SFU vanishes: packets go nowhere
    await asyncio.sleep(1.0)
    t0 = time.perf_counter()
    hung = False
    try:
        await asyncio.wait_for(room.disconnect(), timeout=args.disconnect_timeout)
    except asyncio.TimeoutError:
        hung = True
    disconnect_s = round(time.perf_counter() - t0, 1)
    print(f"    [abandoned] disconnect hung={hung} after {disconnect_s}s — ABANDONING "
          "(D1 declares the Room dead here)", flush=True)
    # D1 says: abandon the await, drop the reference, carry on.
    del room

    docker("unpause", args.container)        # the network heals; the clock resumes
    print("    [abandoned] SFU unpaused — watching the seat from here", flush=True)
    result = await watch_seat(room_name, ident, args.budget, "abandoned")
    return {"arm": "ABANDONED_hung_disconnect", "seated_before": len(before),
            "disconnect_hung": hung, "disconnect_s": disconnect_s,
            "room_name": room_name, "identity": ident, **result}


async def arm_orphan_live(args) -> dict:
    """THE DANGEROUS CASE, and the one D1 actually creates.

    The first ABANDONED arm healed the network, so the buffered leave flushed and the seat
    freed at once — reassuring, but not what D1 leaves behind. D1 abandons the Room inside a
    process that KEEPS RUNNING. Its native WebRTC threads are still alive and may still be
    answering the SFU's keepalives, in which case the server never notices anything is wrong
    and the seat is held FOREVER — an unbounded ghost, not a 20-second one.

    So: connect, drop every reference WITHOUT disconnecting, keep this process alive, and
    watch. No pause, no partition, server clock running throughout.
    """
    room_name = f"seat-orphan-{int(time.time())}"
    ident = "orphan-live-participant"
    room = rtc.Room()
    await room.connect(URL, mint(room_name, ident))
    await asyncio.sleep(3)
    before = await seats(room_name)
    print(f"    [orphan-live] seated: {before}", flush=True)
    print("    [orphan-live] dropping the reference WITHOUT disconnect; process stays alive",
          flush=True)
    del room
    import gc
    gc.collect()
    result = await watch_seat(room_name, ident, args.budget, "orphan-live")
    return {"arm": "ORPHAN_LIVE_no_disconnect_process_alive", "seated_before": len(before),
            "room_name": room_name, "identity": ident, **result}


async def arm_stopped(args) -> dict:
    """Client SIGSTOPped: socket stays open, no RST, no application response.

    The truest model of a hung/partitioned peer with a live TCP connection — the server sees
    a connection it cannot get answers from, which is what a real network stall looks like
    from its side (unlike SIGKILL, where the OS closes the socket for us).
    """
    room_name = f"seat-stopped-{int(time.time())}"
    ident = "stopped-participant"
    child = subprocess.Popen(
        [sys.executable, "-c", f'''
import asyncio, sys
from livekit import rtc, api
async def main():
    room = rtc.Room()
    grants = api.VideoGrants(room_join=True, room={room_name!r}, can_publish=True, can_subscribe=True)
    tok = (api.AccessToken({KEY!r}, {SECRET!r}).with_identity({ident!r})
           .with_name({ident!r}).with_grants(grants).to_jwt())
    await room.connect({URL!r}, tok)
    print("JOINED", flush=True)
    await asyncio.sleep(3600)
asyncio.run(main())
'''], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        line = child.stdout.readline()
        if not line or "JOINED" in line:
            break
    await asyncio.sleep(3)
    before = await seats(room_name)
    print(f"    [stopped] seated: {before}", flush=True)
    import signal as _sig
    child.send_signal(_sig.SIGSTOP)
    print("    [stopped] SIGSTOPped — socket open, no answers", flush=True)
    try:
        result = await watch_seat(room_name, ident, args.budget, "stopped")
    finally:
        child.send_signal(_sig.SIGCONT)
        child.kill()
        child.wait()
        try:
            child.stdout.close()
        except Exception:
            pass
    return {"arm": "STOPPED_sigstop_socket_open", "seated_before": len(before), **result}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=180.0,
                    help="how long to watch for the seat to free itself")
    ap.add_argument("--disconnect-timeout", type=float, default=5.0,
                    help="D1's deadline; default matches the design")
    ap.add_argument("--container", default="lk-spike")
    ap.add_argument("--arms", default="control,killed,abandoned,stopped,orphan_live")
    args = ap.parse_args()

    out = {}
    try:
        arms = args.arms.split(",")
        if "control" in arms:
            print("ARM CONTROL — clean disconnect (positive control: the seat MUST free)", flush=True)
            out["control"] = await arm_control(args)
            if not out["control"]["left"]:
                print("!! CONTROL FAILED — a clean disconnect did not free the seat. Every "
                      "other arm in this run is VOID: the poller cannot see departures.",
                      flush=True)
        if "killed" in arms:
            print("ARM KILLED — client process SIGKILLed", flush=True)
            out["killed"] = await arm_killed(args)
        if "abandoned" in arms:
            print("ARM ABANDONED — D1's hung-disconnect-then-abandon", flush=True)
            out["abandoned"] = await arm_abandoned(args)
        if "stopped" in arms:
            print("ARM STOPPED — client SIGSTOPped, socket open, no answers", flush=True)
            out["stopped"] = await arm_stopped(args)
        if "orphan_live" in arms:
            print("ARM ORPHAN_LIVE — abandoned Room in a STILL-RUNNING process (the D1 case)",
                  flush=True)
            out["orphan_live"] = await arm_orphan_live(args)
        print("SEAT_VERDICT=" + json.dumps(out), flush=True)
        return 0
    finally:
        docker("unpause", args.container)    # never leave the SFU paused


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
