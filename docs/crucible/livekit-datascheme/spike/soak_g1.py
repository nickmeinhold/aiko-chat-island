#!/usr/bin/env python3
"""G1 — is the in-process RSS drift the immortal Rooms, and does the reaper close it?

G1 was the last live doubt about D1: `run_teardown.py` measured flat threads and flat fds but
an unexplained **~0.4 MB/cycle RSS growth**, which is noise over 16 cycles and an OOM over
10,000. It was deferred to "a long soak" with no hypothesis.

F8 handed it a suspect. An abandoned Room in a living process is never reaped by the SFU (no
departure observed in 240s, vs ~22s for a silent peer) because its native threads keep
answering keepalives. So after N hostile teardowns the process is holding N live WebRTC
sessions — each with a decoder, transport and ICE agent. That is exactly the shape of a slow
RSS leak with flat thread and fd counts.

**Hypothesis:** the drift IS the immortal Rooms, and `RemoveParticipant` closes it — because
evicting the seat makes the SERVER tear the session down, which is the one thing that can
unblock a native stack the client can no longer reach.

**Two arms, same cycles, same everything else:**

  NO_REAP  epoch identity, hostile teardown, abandon. Nothing reclaims the seat.
  REAP     identical, plus a `RemoveParticipant` for the abandoned epoch after each cycle.

**A second thing this measures, which matters independently of RSS.** Epoch identities were
introduced so a new bridge cannot collide with the corpse under last-session-wins. But that
means the corpse is no longer *overwritten* — so without a reaper the ghosts **accumulate**,
one per restart, each holding a seat forever. Epoch alone converts a collision into a leak.
The roster count per arm measures exactly that, and it is the stronger argument for the
reaper being mandatory: not "memory grows" but "the room fills up with dead robots".

Env: LK_URL, LK_API_KEY, LK_API_SECRET
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
from reaper import SeatReaper, epoch_identity  # noqa: E402

URL = os.environ["LK_URL"]
KEY = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]
HTTP = URL.replace("ws://", "http://").replace("wss://", "https://")


def mint(room: str, identity: str) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
    return (api.AccessToken(KEY, SECRET).with_identity(identity)
            .with_name(identity).with_grants(grants).to_jwt())


async def roster_size(room: str) -> int:
    lk = api.LiveKitAPI(HTTP, KEY, SECRET)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        return len(res.participants)
    except Exception:
        return -1
    finally:
        await lk.aclose()


def docker(action: str, container: str) -> None:
    subprocess.run(["docker", action, container], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sample(proc) -> dict:
    try:
        fds = proc.num_fds()
    except Exception:
        fds = -1
    return {"threads": proc.num_threads(), "fds": fds,
            "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1)}


async def run_arm(args, reap_enabled: bool) -> dict:
    """N cycles of connect -> hostile teardown -> abandon, with or without the reaper."""
    label = "REAP" if reap_enabled else "NO_REAP"
    room_name = f"soak-{label.lower()}-{int(time.time())}"
    base = "soak-bridge"
    proc = psutil.Process()
    reaper = SeatReaper(URL, KEY, SECRET)
    samples = []

    for i in range(args.cycles):
        identity = epoch_identity(base, i)      # a NEW generation every cycle
        room = rtc.Room()
        await room.connect(URL, mint(room_name, identity))
        await asyncio.sleep(args.hold)

        if args.abandon_mode == "hostile":
            # Black-hole the SFU, race the disconnect, abandon it. NOTE: this does NOT
            # reliably produce an immortal seat — healing the network flushes the buffered
            # leave, so the server sees a clean departure (measured: seats=0 every cycle).
            # Kept because it is D1's literal wording, but it is the WEAK arm.
            docker("pause", args.container)
            await asyncio.sleep(0.3)
            try:
                await asyncio.wait_for(room.disconnect(), timeout=args.disconnect_timeout)
            except asyncio.TimeoutError:
                pass
            docker("unpause", args.container)
        else:
            # ORPHAN: drop every reference with NO disconnect at all, process stays alive.
            # `measure_seat.py` proves THIS is what creates the immortal seat (still ACTIVE
            # at 240s), so it is the only abandonment that actually tests the hypothesis.
            pass
        del room
        gc.collect()

        if reap_enabled:
            # Reclaim the seat this cycle just abandoned. `keep=None`: every prior epoch of
            # this base is stale by definition, since we are between streams.
            result = await reaper.reap(room_name, base, keep=None)
            if result.error:
                print(f"    [{label}] cycle {i+1} reap error: {result.error}", flush=True)

        await asyncio.sleep(args.settle)
        snap = {"cycle": i + 1, "seats": await roster_size(room_name), **sample(proc)}
        samples.append(snap)
        print(f"    [{label}] cycle {i+1}/{args.cycles}  seats={snap['seats']:>3}  "
              f"rss={snap['rss_mb']:>7}MB  threads={snap['threads']}  fds={snap['fds']}",
              flush=True)

    # Baseline from cycle 2: cycle 1 carries one-time native-stack init, and counting that
    # as growth would report setup as a leak.
    base_s, last_s = samples[1], samples[-1]
    span = max(1, last_s["cycle"] - base_s["cycle"])
    return {
        "arm": label, "room": room_name, "cycles": args.cycles,
        "rss_per_cycle_mb": round((last_s["rss_mb"] - base_s["rss_mb"]) / span, 3),
        "threads_per_cycle": round((last_s["threads"] - base_s["threads"]) / span, 3),
        "fds_per_cycle": round((last_s["fds"] - base_s["fds"]) / span, 3),
        "seats_final": last_s["seats"],
        "rss_first": base_s["rss_mb"], "rss_last": last_s["rss_mb"],
        "samples": samples,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--disconnect-timeout", type=float, default=5.0)
    ap.add_argument("--container", default="lk-spike")
    ap.add_argument("--abandon-mode", choices=("orphan", "hostile"), default="orphan",
                    help="orphan = drop the ref with no disconnect (PROVEN to strand the "
                         "seat); hostile = pause/race/unpause (the leave flushes on heal, "
                         "so it does NOT strand the seat -- weak arm, kept for provenance)")
    ap.add_argument("--only", choices=("no_reap", "reap"), default=None,
                    help="run ONE arm in this process. Both arms in one process share a "
                         "baseline -- the second arm inherits whatever the first leaked -- "
                         "so the clean comparison runs each arm in its own process.")
    args = ap.parse_args()

    out = {}
    try:
        # NO_REAP first: it establishes the drift exists in this run before the fix is
        # applied. Running the fix first and finding it flat would prove nothing about
        # whether there was ever anything to fix.
        if args.only in (None, "no_reap"):
            print("ARM NO_REAP — epoch identities, nothing reclaims the seat", flush=True)
            out["no_reap"] = await run_arm(args, reap_enabled=False)
        if args.only in (None, "reap"):
            print("ARM REAP — identical, plus RemoveParticipant after each abandon", flush=True)
            out["reap"] = await run_arm(args, reap_enabled=True)
        if args.only:
            arm = out[args.only]
            print("G1_ARM=" + json.dumps({k: v for k, v in arm.items() if k != "samples"}),
                  flush=True)
            return 0

        nr, r = out["no_reap"], out["reap"]
        out["summary"] = {
            "rss_per_cycle_no_reap": nr["rss_per_cycle_mb"],
            "rss_per_cycle_reap": r["rss_per_cycle_mb"],
            "seats_accumulated_no_reap": nr["seats_final"],
            "seats_accumulated_reap": r["seats_final"],
            "ghosts_accumulate_without_reaper": nr["seats_final"] > 2,
            "reaper_prevents_accumulation": r["seats_final"] <= 2,
        }
        print("G1_VERDICT=" + json.dumps(out["summary"]), flush=True)
        print("G1_FULL=" + json.dumps(out), flush=True)
        return 0
    finally:
        docker("unpause", args.container)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
