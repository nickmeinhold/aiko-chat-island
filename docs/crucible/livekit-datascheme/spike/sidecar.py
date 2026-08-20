#!/usr/bin/env python3
"""The `webrtc://` sidecar — the out-of-process bridge the Temper round-1 re-cast chose.

Shape under test (DESIGN.md re-cast, decision 1): `webrtc://` is a first-class DataScheme
whose create_sources owns a SUPERVISED OUT-OF-PROCESS helper, and frames reach the
pipeline over the already-solved `zmq://` path. This file is that helper. It:

  1. connects to a LiveKit room and subscribes to one video track,
  2. converts each decoded frame to numpy RGB,
  3. pushes it to a ZMQ PULL socket the pipeline side owns.

Socket direction deliberately matches `scheme_zmq`: the DATA SOURCE binds PULL, the
producer connects PUSH. That is what makes the sidecar a drop-in `zmq://` producer
rather than a bespoke protocol — the pipeline half is aiko's existing, already-proven code.

WIRE FORMAT is the live question, so it is a flag rather than a decision:

  --wire jpeg  aiko's actual convention today. `image_io.ImageWriteZMQ` sends
               `image_to_bytes(image)`, which is `PIL.save(format="JPEG")`, and
               `ImageReadZMQ` decodes it. Compatible with the existing pipeline
               elements with ZERO new code — but it puts a SECOND lossy codec in
               series with VP8 and costs a full JPEG transcode per frame.

  --wire raw   length-prefixed raw RGB24. No second codec, no transcode, but it is not
               what `ImageReadZMQ` expects, so it needs a new media type upstream.

Measuring both is the point: the design assumed "frames reach the pipeline over zmq://"
without pricing which of these two it meant.

Env: LK_URL, LK_API_KEY, LK_API_SECRET, LK_ROOM
Prints `SIDECAR_READY` once subscribed, then one `SIDECAR_STATS=<json>` line at exit.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import signal
import struct
import sys
import time

import numpy as np
import psutil
import zmq
from livekit import api, rtc

WIRE_HEADER = struct.Struct("!HHI")  # width, height, payload length


def mint(key: str, secret: str, room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=False, can_subscribe=True)
    return (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .to_jwt()
    )


async def run(args) -> int:
    url = os.environ["LK_URL"]
    key = os.environ["LK_API_KEY"]
    secret = os.environ["LK_API_SECRET"]
    room_name = os.environ["LK_ROOM"]

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PUSH)
    # Never block the media loop on a slow/absent pipeline: bounded queue + drop.
    sock.setsockopt(zmq.SNDHWM, args.hwm)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(args.zmq)

    room = rtc.Room()
    stop = asyncio.Event()
    stats = {"frames_in": 0, "frames_sent": 0, "dropped_hwm": 0,
             "decode_ms": [], "encode_ms": [], "wire_bytes": [], "calibrate_ms": []}

    # CALIBRATION — the same encode, in this same process, BEFORE any LiveKit native
    # media thread exists. Without this control an in-loop encode timing cannot be
    # attributed: a slow number might be the codec, or it might be this process's own
    # media threads contending for the GIL. Measuring both makes the difference readable.
    if args.wire == "jpeg":
        from PIL import Image
        calib = np.zeros((480, 640, 3), dtype=np.uint8)
        calib[:240, :320] = (220, 30, 30)
        calib[240:, 320:] = (30, 200, 60)
        for _ in range(20):
            t = time.perf_counter()
            buf = io.BytesIO()
            Image.fromarray(calib).save(buf, format="JPEG")
            stats["calibrate_ms"].append((time.perf_counter() - t) * 1000)

    async def pump(track: rtc.RemoteVideoTrack):
        stream = rtc.VideoStream(track)
        try:
            async for event in stream:
                if stop.is_set():
                    break
                stats["frames_in"] += 1

                t0 = time.perf_counter()
                # convert() is the decode-to-frames step: this is where a WebRTC
                # transport stops being opaque and becomes aiko-native pixels.
                rgb_frame = event.frame.convert(rtc.VideoBufferType.RGB24)
                arr = np.frombuffer(rgb_frame.data, dtype=np.uint8).reshape(
                    rgb_frame.height, rgb_frame.width, 3
                )
                # COPY before the SDK recycles the native buffer (Temper: cross-thread
                # buffer ownership — a view into a recycled buffer tears silently).
                arr = np.ascontiguousarray(arr)
                stats["decode_ms"].append((time.perf_counter() - t0) * 1000)

                t1 = time.perf_counter()
                if args.wire == "jpeg":
                    from PIL import Image
                    buf = io.BytesIO()
                    Image.fromarray(arr).save(buf, format="JPEG")
                    payload = buf.getvalue()
                else:
                    payload = arr.tobytes()
                stats["encode_ms"].append((time.perf_counter() - t1) * 1000)
                stats["wire_bytes"].append(len(payload))

                msg = WIRE_HEADER.pack(rgb_frame.width, rgb_frame.height, len(payload)) + payload
                try:
                    sock.send(msg, flags=zmq.NOBLOCK)
                    stats["frames_sent"] += 1
                except zmq.Again:
                    stats["dropped_hwm"] += 1
        finally:
            await stream.aclose()

    pumps: list[asyncio.Task] = []

    @room.on("track_subscribed")
    def _on_track(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            print(f"SIDECAR_SUBSCRIBED from={participant.identity}", flush=True)
            pumps.append(asyncio.create_task(pump(track)))

    await room.connect(url, mint(key, secret, room_name, args.identity),
                       options=rtc.RoomOptions(auto_subscribe=True))
    print("SIDECAR_READY", flush=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # WHOLE-PROCESS CPU, not just the Python-visible steps. `frame.convert()` measures
    # the I420->RGB24 conversion ONLY — the VP8 decode itself already happened on a
    # native SDK thread before the frame reached this async iterator, so no in-loop timer
    # can see it. Process CPU time is the one instrument that counts the native threads,
    # and it is what answers "does a bridged stream really cost about a core?".
    proc = psutil.Process()
    cpu_t0, wall_t0 = sum(proc.cpu_times()[:2]), time.perf_counter()

    try:
        await asyncio.wait_for(stop.wait(), timeout=args.max_seconds)
    except asyncio.TimeoutError:
        stop.set()

    cpu_used, wall_used = sum(proc.cpu_times()[:2]) - cpu_t0, time.perf_counter() - wall_t0

    for task in pumps:
        task.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)
    await room.disconnect()
    sock.close()
    ctx.term()

    def summary(values):
        if not values:
            return None
        arr = np.array(values)
        return {"n": int(arr.size), "mean": round(float(arr.mean()), 3),
                "p95": round(float(np.percentile(arr, 95)), 3)}

    print("SIDECAR_STATS=" + json.dumps({
        "wire": args.wire,
        "frames_in": stats["frames_in"],
        "frames_sent": stats["frames_sent"],
        "dropped_hwm": stats["dropped_hwm"],
        "decode_ms": summary(stats["decode_ms"]),
        "encode_ms": summary(stats["encode_ms"]),
        "calibrate_ms": summary(stats["calibrate_ms"]),
        "wire_bytes": summary(stats["wire_bytes"]),
        "cpu_seconds": round(cpu_used, 3),
        "wall_seconds": round(wall_used, 3),
        "cores_used": round(cpu_used / wall_used, 3) if wall_used > 0 else None,
        "fps_observed": round(stats["frames_in"] / wall_used, 2) if wall_used > 0 else None,
    }), flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zmq", default="tcp://127.0.0.1:6502", help="PULL endpoint to connect to")
    ap.add_argument("--wire", choices=("jpeg", "raw"), default="jpeg")
    ap.add_argument("--identity", default="webrtc-sidecar")
    ap.add_argument("--hwm", type=int, default=8)
    ap.add_argument("--max-seconds", type=float, default=300.0,
                    help="self-terminate backstop so an orphaned sidecar cannot run forever")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
