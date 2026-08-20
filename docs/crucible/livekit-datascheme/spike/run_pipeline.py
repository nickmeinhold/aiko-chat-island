#!/usr/bin/env python3
"""M4 — the integration proof: a REAL aiko Pipeline reading from a REAL LiveKit room.

Everything before this measured the frame path and teardown behaviour of a bridge that
stood beside aiko. This runs the actual thing the design is about:

    publisher --VP8--> SFU --VP8--> [ aiko_pipeline: ImageReadZMQ -> PatternAssert ]
                                      ^ dispatched by DataScheme.LOOKUP["webrtc"]

with a real broker, a real `aiko_pipeline create`, a real Stream, and aiko's OWN
unmodified `ImageReadZMQ` as the source element. Nothing here stands in for the pipeline.

The acceptance gate is `PIPELINE_PIXEL_OK` from `PatternAssert`, which checks images the
pipeline actually delivered against the independently generated known pattern. A pipeline
that runs but carries garbage must fail, so "it started" is not the gate.

Needs: mosquitto on $AIKO_MQTT_PORT (default 1884), a LiveKit SFU at $LK_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wire", choices=("jpeg", "raw"), default="jpeg")
    ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--hostile-stop", action="store_true",
                    help="Black-hole the SFU before stopping the pipeline. Exercises the "
                         "SIGTERM->SIGKILL escalation in destroy_sources on the REAL path: "
                         "the sidecar is measured to ignore SIGTERM indefinitely in this "
                         "state, so a supervisor that does not escalate leaves an orphan.")
    ap.add_argument("--container", default="lk-spike")
    ap.add_argument("--grace-before-stop", type=float, default=0.0,
                    help="Seconds to let the pipeline run after the verdict, so aiko's "
                         "own delayed destroy_stream can fire before we terminate.")
    ap.add_argument("--no-publisher", action="store_true",
                    help="NEGATIVE CONTROL: run with nothing publishing. A gate that "
                         "cannot go red proves nothing, so this must NOT reach "
                         "PIPELINE_PIXEL_OK -- create_sources should fail closed.")
    args = ap.parse_args()

    room = f"spike-pipeline-{int(time.time())}"
    env = dict(os.environ, LK_ROOM=room, LK_W="640", LK_H="480")
    env.setdefault("AIKO_MQTT_PORT", "1884")
    env.setdefault("AIKO_MQTT_HOST", "localhost")
    # The pipeline's element module and its sibling imports live here.
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")

    # Materialise the definition with this run's room. Written beside the template rather
    # than into it so the committed definition keeps its placeholder.
    with open(os.path.join(HERE, "pipeline_webrtc.json")) as fp:
        definition = json.load(fp)
    for element in definition["elements"]:
        params = element.get("parameters", {})
        if "data_sources" in params:
            params["data_sources"] = params["data_sources"].replace("SPIKE_ROOM", room)
            params["wire"] = args.wire
        if "assert_frames" in params:
            params["assert_frames"] = args.frames
    run_definition = os.path.join(HERE, ".pipeline_webrtc.run.json")
    with open(run_definition, "w") as fp:
        json.dump(definition, fp, indent=2)

    pub = None
    if not args.no_publisher:
        pub = subprocess.Popen([sys.executable, os.path.join(HERE, "publisher.py")], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pipeline = None
    try:
        if pub:
            deadline = time.time() + 60
            while time.time() < deadline:
                line = pub.stdout.readline()
                if not line or "PUBLISHER_READY" in line:
                    break
            print(f"[harness] publisher up, room={room}", flush=True)
        else:
            print(f"[harness] NEGATIVE CONTROL: no publisher, room={room}", flush=True)

        pipeline = subprocess.Popen(
            # `-s 1` is REQUIRED, not decoration: without a stream id no Stream is
            # created, so DataSource.start_stream never fires and the scheme is never
            # dispatched — the pipeline sits silent looking healthy. `-lm false` sends
            # logs to the console instead of MQTT so this harness can read them.
            [os.path.join(os.path.dirname(sys.executable), "aiko_pipeline"),
             "create", run_definition, "-s", "1", "-sr", "-ll", "info", "-lm", "false",
             "-gt", str(int(args.timeout))],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        verdict, frames, deadline = None, [], time.time() + args.timeout
        while time.time() < deadline:
            line = pipeline.stdout.readline()
            if not line:
                if pipeline.poll() is not None:
                    break
                continue
            sys.stderr.write(f"[pipeline] {line}")
            if line.startswith("PIPELINE_FRAME"):
                frames.append(line.strip())
            if "PIPELINE_VERDICT=" in line:
                verdict = re.search(r"PIPELINE_VERDICT=(\S+)", line).group(1)
                break

        # aiko schedules destroy_stream with delay=3.0 after a STOP, so terminating the
        # pipeline immediately would race it and report "never called" unfairly.
        if args.grace_before_stop:
            print(f"[harness] waiting {args.grace_before_stop}s for aiko's own "
                  "destroy_stream before shutting down", flush=True)
            end = time.time() + args.grace_before_stop
            while time.time() < end and pipeline.poll() is None:
                line = pipeline.stdout.readline()
                if line:
                    sys.stderr.write(f"[pipeline] {line}")

        # ORPHAN CHECK — must run AFTER the pipeline is reaped. A sidecar still running
        # while its parent pipeline is alive is correct, not a leak, so checking here
        # before shutdown would report a false positive every time.
        if args.hostile_stop:
            print("[harness] HOSTILE STOP: pausing the SFU before pipeline shutdown", flush=True)
            subprocess.run(["docker", "pause", args.container], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        if pipeline.poll() is None:
            pipeline.terminate()
            try:
                pipeline.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pipeline.kill()
                pipeline.wait()
        if args.hostile_stop:
            subprocess.run(["docker", "unpause", args.container], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)
        orphans = subprocess.run(
            ["pgrep", "-f", "sidecar.py.*webrtc-scheme"],
            capture_output=True, text=True).stdout.split()
        print(json.dumps({
            "room": room, "wire": args.wire,
            "hostile_stop": args.hostile_stop,
            "orphaned_sidecars_after_run": len(orphans),
            "frames_delivered_by_pipeline": len(frames),
            "verdict": verdict or "NO_VERDICT",
            "detail": frames[:5],
        }, indent=2), flush=True)
        return 0 if verdict == "PIPELINE_PIXEL_OK" else 3
    finally:
        for proc in (pipeline, pub):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc:
                try:
                    proc.stdout.close()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
