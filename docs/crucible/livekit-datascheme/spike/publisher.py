#!/usr/bin/env python3
"""Publish the known-answer test pattern into a LiveKit room.

Stands in for whatever is on the far side of the room — the app's camera, a robot's
camera. Deliberately a SEPARATE PROCESS from the sidecar under test, so the frames the
sidecar receives have genuinely crossed an SFU and a lossy codec rather than being
handed over in-process (a self-roundtrip proves self-consistency, not correctness).

Env: LK_URL, LK_API_KEY, LK_API_SECRET, LK_ROOM, LK_W, LK_H, LK_FPS, LK_IDENTITY
Prints `PUBLISHER_READY` on stdout once the track is published.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pattern  # noqa: E402
from livekit import api, rtc  # noqa: E402

URL = os.environ["LK_URL"]
KEY = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]
ROOM = os.environ["LK_ROOM"]
W = int(os.environ.get("LK_W", "640"))
H = int(os.environ.get("LK_H", "480"))
FPS = int(os.environ.get("LK_FPS", "15"))
IDENTITY = os.environ.get("LK_IDENTITY", "spike-publisher")


def mint() -> str:
    grants = api.VideoGrants(room_join=True, room=ROOM, can_publish=True, can_subscribe=False)
    return (
        api.AccessToken(KEY, SECRET)
        .with_identity(IDENTITY)
        .with_name(IDENTITY)
        .with_grants(grants)
        .to_jwt()
    )


async def main() -> int:
    room = rtc.Room()
    await room.connect(URL, mint(), options=rtc.RoomOptions(auto_subscribe=False))

    source = rtc.VideoSource(W, H)
    track = rtc.LocalVideoTrack.create_video_track("spike-pattern", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
    )
    print("PUBLISHER_READY", flush=True)

    # Pre-build the frames: RGBA is the buffer type the proven in-repo probe uses, and
    # building them up front keeps the pump loop off the CPU so frame TIMING measured
    # downstream reflects transport, not this process's own encode.
    frames = []
    for seq in range(pattern.MARKER_BITS * 0 + 32):
        rgb = pattern.make(W, H, seq)
        rgba = np.dstack([rgb, np.full((H, W, 1), 255, dtype=np.uint8)])
        frames.append(rtc.VideoFrame(W, H, rtc.VideoBufferType.RGBA, rgba.tobytes()))

    seq = 0
    try:
        while True:
            source.capture_frame(frames[seq % len(frames)])
            seq += 1
            await asyncio.sleep(1 / FPS)
    except asyncio.CancelledError:
        pass
    finally:
        await room.disconnect()
    return 0


try:
    sys.exit(asyncio.run(main()))
except KeyboardInterrupt:
    sys.exit(0)
