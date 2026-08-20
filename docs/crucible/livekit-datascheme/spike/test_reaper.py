#!/usr/bin/env python3
"""Prove the reaper against a REAL immortal orphan, and prove it cannot overreach.

Red then green: `measure_seat.py` establishes that an abandoned Room in a living process holds
its seat indefinitely (still ACTIVE at 240s+, against ~22s for a merely silent peer). This
creates exactly that ghost and then reaps it.

A reaper that works is only half the requirement. The other half is that it CANNOT do more
than reclaim its own identity — it authenticates with API master credentials, so an
over-broad reaper is a room-wide kick primitive on every host that runs a pipeline. Hence the
negative controls: a bystander's seat and the `keep` identity must both survive.

Env: LK_URL, LK_API_KEY, LK_API_SECRET
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from livekit import api, rtc  # noqa: E402
from reaper import SeatReaper, base_identity, epoch_identity  # noqa: E402

URL = os.environ["LK_URL"]
KEY = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]
HTTP = URL.replace("ws://", "http://").replace("wss://", "https://")


def mint(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (api.AccessToken(KEY, SECRET).with_identity(identity)
            .with_name(identity).with_grants(grants).to_jwt())


async def roster(room: str) -> list:
    lk = api.LiveKitAPI(HTTP, KEY, SECRET)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        return sorted(p.identity for p in res.participants)
    except Exception:
        return []
    finally:
        await lk.aclose()


async def main() -> int:
    room_name = f"reaper-test-{int(time.time())}"
    base = "robot-7"
    results = {}

    # --- make an IMMORTAL ghost: connect, abandon the Room, keep this process alive -------
    ghost_identity = epoch_identity(base, 1)
    ghost = rtc.Room()
    await ghost.connect(URL, mint(room_name, ghost_identity))

    # --- a BYSTANDER that the reaper must never touch -------------------------------------
    bystander = rtc.Room()
    await bystander.connect(URL, mint(room_name, "someone-else"))

    await asyncio.sleep(3)
    del ghost                      # abandoned: no disconnect, native threads still answering
    import gc
    gc.collect()
    await asyncio.sleep(2)

    before = await roster(room_name)
    results["roster_before"] = before
    print(f"roster before reap: {before}", flush=True)

    # RED: confirm the ghost really is still seated (not merely slow to appear).
    still_there = ghost_identity in before
    results["ghost_seated_before_reap"] = still_there
    if not still_there:
        print("!! RED CONTROL FAILED: no ghost to reap — this run proves nothing.", flush=True)

    # --- the new bridge's identity: epoch N+1, which must be untouched --------------------
    new_identity = epoch_identity(base, 2)
    newcomer = rtc.Room()
    await newcomer.connect(URL, mint(room_name, new_identity))
    await asyncio.sleep(2)
    print(f"roster with newcomer: {await roster(room_name)}", flush=True)

    # --- REAP ----------------------------------------------------------------------------
    reaper = SeatReaper(URL, KEY, SECRET)
    t0 = time.perf_counter()
    result = await reaper.reap(room_name, base, keep=new_identity)
    reap_s = round(time.perf_counter() - t0, 2)
    print(f"reap: evicted={result.evicted} skipped={result.skipped} "
          f"error={result.error} in {reap_s}s", flush=True)

    await asyncio.sleep(3)
    after = await roster(room_name)
    results["roster_after"] = after
    print(f"roster after reap:  {after}", flush=True)

    results.update({
        "reap_seconds": reap_s,
        "evicted": list(result.evicted),
        "skipped": list(result.skipped),
        "error": result.error,
        # GREEN: the immortal ghost is gone...
        "ghost_evicted": ghost_identity not in after,
        # ...and the reaper did NOT overreach:
        "bystander_survived": "someone-else" in after,
        "newcomer_survived": new_identity in after,
    })

    # --- a reap against a room that does not exist is a SUCCESS, not an error -------------
    empty = await reaper.reap(f"no-such-room-{int(time.time())}", base)
    results["missing_room_ok"] = empty.ok and empty.evicted == []

    passed = (results["ghost_seated_before_reap"] and results["ghost_evicted"]
              and results["bystander_survived"] and results["newcomer_survived"]
              and results["missing_room_ok"] and result.error is None)
    results["result"] = "REAPER_OK" if passed else "REAPER_FAIL"
    print("REAPER_VERDICT=" + json.dumps(results), flush=True)

    await newcomer.disconnect()
    await bystander.disconnect()
    return 0 if passed else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
