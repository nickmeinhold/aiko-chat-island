#!/usr/bin/env python3
"""M1 — the PRIMARY falsifier, measured instead of argued.

Temper round 1 overturned the design's mechanism on this claim:

    in-process asyncio `Room` teardown genuinely leaks. `scheme_zmq` is safe only
    because `zmq.RCVTIMEO` forces the blocking recv to observe the terminate flag;
    asyncio + native WebRTC/FFI threads give no equivalent guarantee, so a hung
    `disconnect()` orphans the thread/loop/peer-connections and restart storms leak.

Three model families agreed on that, and it flipped the default from an in-process
DataScheme to a supervised out-of-process sidecar. Nobody ran it. This does.

A pipeline stream is created and destroyed many times over a long-lived process, so the
question is not "does one teardown work" but "do N teardowns leave the process where they
found it". Both arms run the SAME cycle count against the SAME live room:

  in-process    connect a Room / subscribe / carry frames / disconnect / drop the ref
  out-of-process  spawn sidecar.py / wait ready / carry frames / SIGTERM / reap

and after each cycle we sample OS-level resources the leak would show up in — thread
count, open file descriptors, RSS. Python-level object counts are deliberately not the
measure: the leak under test is native threads and sockets, which gc has no view of.

Run with an already-running publisher, or let --spawn-publisher handle it.
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


def sample(proc: psutil.Process) -> dict:
    """OS-level resource snapshot — the layer a native-thread leak is visible at."""
    try:
        fds = proc.num_fds()
    except Exception:
        fds = -1
    return {
        "threads": proc.num_threads(),
        "fds": fds,
        "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
    }


def mint(key, secret, room, identity) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=False, can_subscribe=True)
    return (api.AccessToken(key, secret).with_identity(identity)
            .with_name(identity).with_grants(grants).to_jwt())


async def in_process_cycles(args, env, proc, samples) -> None:
    """Arm A — the mechanism the Temper rejected, exercised honestly."""
    for i in range(args.cycles):
        room = rtc.Room()
        frames = {"n": 0}
        pumps = []

        async def pump(track):
            stream = rtc.VideoStream(track)
            try:
                async for _ in stream:
                    frames["n"] += 1
            except asyncio.CancelledError:
                pass
            finally:
                await stream.aclose()

        @room.on("track_subscribed")
        def _on(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_VIDEO:
                pumps.append(asyncio.create_task(pump(track)))

        await room.connect(env["LK_URL"], mint(env["LK_API_KEY"], env["LK_API_SECRET"],
                                               env["LK_ROOM"], f"inproc-{i}"),
                           options=rtc.RoomOptions(auto_subscribe=True))
        await asyncio.sleep(args.hold)
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        await room.disconnect()
        del room, pumps
        gc.collect()
        await asyncio.sleep(0.5)   # give native threads a chance to actually unwind
        samples.append({"cycle": i + 1, "frames": frames["n"], **sample(proc)})
        print(f"  in-process  cycle {i+1}/{args.cycles} {samples[-1]}", flush=True)


def out_of_process_cycles(args, env, proc, samples) -> None:
    """Arm B — the consensus re-cast: the OS reaps what Python cannot."""
    for i in range(args.cycles):
        side = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "sidecar.py"), "--wire", "raw",
             "--zmq", f"tcp://127.0.0.1:{args.port}", "--identity", f"sidecar-{i}",
             "--max-seconds", str(args.hold + 30)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            line = side.stdout.readline()
            if not line or "SIDECAR_READY" in line:
                break
        time.sleep(args.hold)
        side.terminate()
        try:
            side.wait(timeout=15)
        except subprocess.TimeoutExpired:
            side.kill()
            side.wait()
        # The harness must not leak what it is measuring: an unclosed Popen stdout pipe
        # costs the PARENT one fd per cycle, which reads exactly like the leak under test.
        try:
            side.stdout.close()
        except Exception:
            pass
        # Likewise drain the PULL socket -- undrained 900KiB frames accumulate in zmq's
        # receive buffer and show up as parent RSS growth attributable to nothing.
        drained = 0
        while True:
            try:
                args.pull.recv(flags=__import__("zmq").NOBLOCK)
                drained += 1
            except Exception:
                break
        time.sleep(0.5)
        samples.append({"cycle": i + 1, "child_rc": side.returncode,
                        "drained": drained, **sample(proc)})
        print(f"  out-of-proc cycle {i+1}/{args.cycles} {samples[-1]}", flush=True)


def verdict(samples: list, label: str, cycles: int) -> dict:
    """Compare the LAST cycle against a settled baseline.

    Cycle 1 is not the baseline: the first connection lazily initialises the native
    stack, so measuring growth from it would report one-time setup as a leak. The
    baseline is cycle 2, and growth is judged over the cycles after it.
    """
    if len(samples) < 3:
        return {"label": label, "verdict": "INSUFFICIENT_CYCLES"}
    base, last = samples[2], samples[-1]
    grew = {k: last[k] - base[k] for k in ("threads", "fds", "rss_mb")}
    per_cycle = {k: round(v / max(1, last["cycle"] - base["cycle"]), 2) for k, v in grew.items()}
    # A leak of native threads/fds is what the claim is about — a thread or fd per cycle
    # is unambiguous. RSS is reported but NOT part of the verdict: allocator behaviour
    # makes a few MB of drift normal and it would produce false positives.
    leaking = per_cycle["threads"] >= 0.5 or per_cycle["fds"] >= 1.0
    return {
        "label": label,
        "baseline_cycle": base["cycle"], "final_cycle": last["cycle"],
        "baseline": {k: base[k] for k in ("threads", "fds", "rss_mb")},
        "final": {k: last[k] for k in ("threads", "fds", "rss_mb")},
        "growth_total": grew, "growth_per_cycle": per_cycle,
        "verdict": "LEAKS" if leaking else "FLAT",
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=12)
    ap.add_argument("--hold", type=float, default=2.0, help="seconds of live media per cycle")
    ap.add_argument("--port", type=int, default=6700)
    ap.add_argument("--arm", choices=("in", "out", "both"), default="both")
    args = ap.parse_args()

    room_name = f"spike-teardown-{int(time.time())}"
    env = dict(os.environ, LK_ROOM=room_name, LK_W="640", LK_H="480")
    proc = psutil.Process()

    # A PULL socket must exist for arm B's sidecar to push into, else zmq queues in the
    # child and the arms are not comparable.
    import zmq
    ctx = zmq.Context()
    pull = ctx.socket(zmq.PULL)
    pull.setsockopt(zmq.RCVHWM, 4)
    pull.bind(f"tcp://127.0.0.1:{args.port}")
    args.pull = pull

    pub = subprocess.Popen([sys.executable, os.path.join(HERE, "publisher.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = {"room": room_name, "cycles": args.cycles, "hold_seconds": args.hold}
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = pub.stdout.readline()
            if not line or "PUBLISHER_READY" in line:
                break

        if args.arm in ("in", "both"):
            print("ARM A — in-process Room create/destroy (the mechanism the Temper rejected)", flush=True)
            samples_in: list = []
            samples_in.append({"cycle": 0, **sample(proc)})
            await in_process_cycles(args, env, proc, samples_in)
            out["in_process"] = verdict(samples_in, "in_process", args.cycles)
            out["in_process_samples"] = samples_in

        if args.arm in ("out", "both"):
            print("ARM B — out-of-process sidecar spawn/reap (the consensus re-cast)", flush=True)
            samples_out: list = []
            samples_out.append({"cycle": 0, **sample(proc)})
            out_of_process_cycles(args, env, proc, samples_out)
            out["out_of_process"] = verdict(samples_out, "out_of_process", args.cycles)
            out["out_of_process_samples"] = samples_out

        print("TEARDOWN_VERDICT=" + json.dumps(out), flush=True)
        return 0
    finally:
        if pub.poll() is None:
            pub.terminate()
            try:
                pub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pub.kill()
        try:
            pub.stdout.close()
        except Exception:
            pass
        pull.close()
        ctx.term()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
