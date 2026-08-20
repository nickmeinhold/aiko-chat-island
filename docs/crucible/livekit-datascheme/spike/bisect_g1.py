#!/usr/bin/env python3
"""G1 residual — WHICH STAGE of a connect cycle retains ~0.4 MB?

The clue is the stability. Across four independent runs the residual drift is
0.389 / 0.400 / 0.422 / 0.428 MB per cycle — under clean close, under stranding, and under
eviction alike. A leak indifferent to *how the session ended* is probably not in the session.

So bisect the cycle instead of soaking it. Each arm does strictly less than the one above,
and the arm where the drift disappears is the arm that contains it:

  FULL          mint -> Room() -> connect -> hold -> clean disconnect
  CONNECT_ONLY  mint -> Room() -> connect -> immediate clean disconnect (no hold, no media)
  ROOM_ONLY     mint -> Room()  ... never connected, just constructed and dropped
  MINT_ONLY     mint a token. No Room object at all.
  NOTHING       an empty loop. THE NULL CONTROL.

NOTHING is the arm that matters most, and it is why this bisect is trustworthy where a soak
is not: if an empty loop also "leaks" ~0.4 MB/cycle, then the number was never about LiveKit
at all — it is Python's allocator, psutil's own sampling, or RSS simply not being a leak
detector at this resolution. Every previous G1 measurement lacked that control, so none of
them could distinguish "a real leak" from "RSS goes up a bit when a process does work".

Reading it: subtract adjacent arms. FULL minus CONNECT_ONLY is what the hold+media costs;
CONNECT_ONLY minus ROOM_ONLY is what connecting costs; and anything still present in
NOTHING was never a leak.

Env: LK_URL, LK_API_KEY, LK_API_SECRET
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sys
import time

import psutil
from livekit import api, rtc

URL = os.environ["LK_URL"]
KEY = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]


def mint(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (api.AccessToken(KEY, SECRET).with_identity(identity)
            .with_name(identity).with_grants(grants).to_jwt())


def rss_mb(proc) -> float:
    return round(proc.memory_info().rss / (1024 * 1024), 2)


async def cycle_full(i: int, room_name: str, hold: float) -> None:
    room = rtc.Room()
    await room.connect(URL, mint(room_name, f"bisect-{i}"))
    await asyncio.sleep(hold)
    await room.disconnect()


async def cycle_connect_only(i: int, room_name: str, hold: float) -> None:
    room = rtc.Room()
    await room.connect(URL, mint(room_name, f"bisect-{i}"))
    await room.disconnect()


async def cycle_room_only(i: int, room_name: str, hold: float) -> None:
    room = rtc.Room()          # constructed, never connected
    del room


async def cycle_mint_only(i: int, room_name: str, hold: float) -> None:
    mint(room_name, f"bisect-{i}")


async def cycle_nothing(i: int, room_name: str, hold: float) -> None:
    await asyncio.sleep(0.01)


ARMS = {
    "FULL": cycle_full,
    "CONNECT_ONLY": cycle_connect_only,
    "ROOM_ONLY": cycle_room_only,
    "MINT_ONLY": cycle_mint_only,
    "NOTHING": cycle_nothing,
}


async def run_arm(name: str, args) -> dict:
    proc = psutil.Process()
    room_name = f"bisect-{name.lower()}-{int(time.time())}"
    fn = ARMS[name]
    samples = []
    for i in range(args.cycles):
        await fn(i, room_name, args.hold)
        gc.collect()
        await asyncio.sleep(args.settle)
        samples.append({"cycle": i + 1, "rss_mb": rss_mb(proc), "fds": proc.num_fds(),
                        "threads": proc.num_threads()})
    # Baseline from cycle 2 — cycle 1 carries one-time init in the arms that have any.
    base, last = samples[1], samples[-1]
    span = max(1, last["cycle"] - base["cycle"])
    out = {
        "arm": name,
        "rss_per_cycle_mb": round((last["rss_mb"] - base["rss_mb"]) / span, 3),
        "fds_per_cycle": round((last["fds"] - base["fds"]) / span, 3),
        "rss_first": base["rss_mb"], "rss_last": last["rss_mb"],
        "cycles": args.cycles,
        # Per-cycle samples matter for the FINAL question a rate cannot answer: is the
        # growth LINEAR (a real leak) or ASYMPTOTIC (a pool/arena warming up)? A single
        # MB/cycle number looks identical either way.
        "samples": samples,
    }
    print(f"  {name:14} rss/cycle={out['rss_per_cycle_mb']:>7} MB   "
          f"({out['rss_first']} -> {out['rss_last']} MB)   fds/cycle={out['fds_per_cycle']}",
          flush=True)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=25)
    ap.add_argument("--hold", type=float, default=1.0)
    ap.add_argument("--settle", type=float, default=0.3)
    ap.add_argument("--arm", default=None, help="run ONE arm (own process = clean baseline)")
    args = ap.parse_args()

    names = [args.arm] if args.arm else list(ARMS)
    print(f"G1 bisect — {args.cycles} cycles per arm", flush=True)
    results = {}
    for name in names:
        results[name] = await run_arm(name, args)
    print("BISECT=" + json.dumps(results), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
