#!/usr/bin/env python3
"""M1b — the falsifier's ACTUAL claim: teardown when `disconnect()` cannot complete.

`run_teardown.py` exercises the clean path and finds both arms flat. That is a real
result but it is NOT the claim the Temper made. The claim was narrower and nastier:

    "a HUNG disconnect() orphans the thread/loop/peer-connections and restart storms leak"

A clean disconnect has a live server on the other end answering the close handshake. The
contested case is a disconnect with nothing on the other end — the SFU is gone, the socket
is black-holed, and asyncio has no `RCVTIMEO` equivalent to force the native side to
observe a terminate flag. Testing the happy path and reporting the claim as refuted would
be exactly the priority-inversion failure: the evidence strongest where the stakes are
lowest.

So this black-holes the SFU with `docker pause` (deliberately NOT `docker stop` — a stop
sends a TCP RST, which completes the close promptly and is the EASY failure; a pause makes
packets vanish, which is what actually hangs a connection) and then tears down.

Arm A  in-process: does `await room.disconnect()` return? If not, what leaks per cycle?
Arm B  out-of-process: SIGTERM the sidecar. Does the OS reap it regardless?

The interesting number is per-cycle growth in threads/fds under REPEATED hostile teardown.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psutil  # noqa: E402
from livekit import api, rtc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sample(proc):
    try:
        fds = proc.num_fds()
    except Exception:
        fds = -1
    return {"threads": proc.num_threads(), "fds": fds,
            "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1)}


def docker(action: str, container: str) -> None:
    subprocess.run(["docker", action, container], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mint(env, identity, can_pub=False):
    grants = api.VideoGrants(room_join=True, room=env["LK_ROOM"],
                             can_publish=can_pub, can_subscribe=True)
    return (api.AccessToken(env["LK_API_KEY"], env["LK_API_SECRET"])
            .with_identity(identity).with_name(identity).with_grants(grants).to_jwt())


async def arm_in_process(args, env, proc):
    samples, hangs = [], 0
    samples.append({"cycle": 0, **sample(proc)})
    for i in range(args.cycles):
        room = rtc.Room()
        await room.connect(env["LK_URL"], mint(env, f"hostile-{i}"),
                           options=rtc.RoomOptions(auto_subscribe=True))
        await asyncio.sleep(args.hold)

        docker("pause", args.container)          # the SFU vanishes mid-session
        await asyncio.sleep(0.3)
        t0 = time.perf_counter()
        hung = False
        try:
            await asyncio.wait_for(room.disconnect(), timeout=args.disconnect_timeout)
        except asyncio.TimeoutError:
            hung = True
            hangs += 1
        disconnect_s = round(time.perf_counter() - t0, 2)
        docker("unpause", args.container)

        del room
        gc.collect()
        await asyncio.sleep(1.0)
        samples.append({"cycle": i + 1, "disconnect_s": disconnect_s,
                        "hung": hung, **sample(proc)})
        print(f"  hostile in-process  cycle {i+1}/{args.cycles} {samples[-1]}", flush=True)
    return samples, hangs


def arm_out_of_process(args, env, proc):
    samples, hangs = [], 0
    samples.append({"cycle": 0, **sample(proc)})
    for i in range(args.cycles):
        side = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "sidecar.py"), "--wire", "raw",
             "--zmq", f"tcp://127.0.0.1:{args.port}", "--identity", f"hostile-side-{i}",
             "--max-seconds", str(args.hold + 60)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            line = side.stdout.readline()
            if not line or "SIDECAR_READY" in line:
                break
        time.sleep(args.hold)

        docker("pause", args.container)
        time.sleep(0.3)
        t0 = time.perf_counter()
        side.terminate()
        hung = False
        try:
            side.wait(timeout=args.disconnect_timeout)
        except subprocess.TimeoutExpired:
            hung = True
            hangs += 1
            side.kill()          # this is the whole point of arm B: the OS always wins
            side.wait()
        teardown_s = round(time.perf_counter() - t0, 2)
        docker("unpause", args.container)

        try:
            side.stdout.close()
        except Exception:
            pass
        time.sleep(1.0)
        samples.append({"cycle": i + 1, "teardown_s": teardown_s, "hung": hung,
                        "rc": side.returncode, **sample(proc)})
        print(f"  hostile out-of-proc cycle {i+1}/{args.cycles} {samples[-1]}", flush=True)
    return samples, hangs


def verdict(samples, label, hangs):
    if len(samples) < 4:
        return {"label": label, "verdict": "INSUFFICIENT_CYCLES"}
    base, last = samples[2], samples[-1]
    span = max(1, last["cycle"] - base["cycle"])
    grew = {k: round(last[k] - base[k], 1) for k in ("threads", "fds", "rss_mb")}
    per = {k: round(v / span, 2) for k, v in grew.items()}
    return {"label": label, "hung_teardowns": hangs, "cycles": len(samples) - 1,
            "baseline": {k: base[k] for k in ("threads", "fds", "rss_mb")},
            "final": {k: last[k] for k in ("threads", "fds", "rss_mb")},
            "growth_per_cycle": per,
            "verdict": "LEAKS" if (per["threads"] >= 0.5 or per["fds"] >= 1.0) else "FLAT"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=8)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--disconnect-timeout", type=float, default=10.0)
    ap.add_argument("--container", default="lk-spike")
    ap.add_argument("--port", type=int, default=6800)
    args = ap.parse_args()

    env = dict(os.environ, LK_ROOM=f"spike-hostile-{int(time.time())}", LK_W="640", LK_H="480")
    proc = psutil.Process()

    import zmq
    ctx = zmq.Context()
    pull = ctx.socket(zmq.PULL)
    pull.setsockopt(zmq.RCVHWM, 2)
    pull.bind(f"tcp://127.0.0.1:{args.port}")

    out = {"cycles": args.cycles, "disconnect_timeout": args.disconnect_timeout}
    try:
        print("ARM A — HOSTILE in-process teardown (SFU black-holed mid-session)", flush=True)
        s_in, h_in = await arm_in_process(args, env, proc)
        out["in_process"] = verdict(s_in, "in_process_hostile", h_in)
        out["in_process_samples"] = s_in

        print("ARM B — HOSTILE out-of-process teardown", flush=True)
        s_out, h_out = arm_out_of_process(args, env, proc)
        out["out_of_process"] = verdict(s_out, "out_of_process_hostile", h_out)
        out["out_of_process_samples"] = s_out

        print("HOSTILE_VERDICT=" + json.dumps(out), flush=True)
        return 0
    finally:
        docker("unpause", args.container)   # never leave the SFU paused
        pull.close()
        ctx.term()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
