#!/usr/bin/env python3
"""M2 + M3 — does a frame survive the bridge, and what does the hop actually cost?

Stands up the whole shape end to end against a real SFU:

    publisher process --VP8--> SFU --VP8--> sidecar process --zmq--> THIS process

and then asks two questions the design answered by reasoning rather than measurement:

  M2  Does the numpy frame arriving on the pipeline side actually correspond, pixel-wise,
      to the frame that was published? Asserted against a KNOWN pattern generated
      independently on this side — never against the transport's own inverse.

  M3  What does the decode-to-frames hop cost per frame, and how much of that is aiko's
      JPEG zmq convention rather than WebRTC itself? This is the measurement behind the
      question worth putting to Andy: is decode-to-frames the deliberate uniform
      DataScheme semantics, or would `webrtc://` want an opaque passthrough type?

Exit 0 only if a frame arrived AND its quadrant error is inside the lossy-codec budget.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pattern  # noqa: E402
import zmq  # noqa: E402
from sidecar import WIRE_HEADER  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# A quadrant of flat colour survives VP8 with a mean error of a couple of levels. A
# channel swap moves a quadrant by ~150 levels; a row flip, similar. 25 sits far above
# codec noise and far below any real adapter bug — the gap is what makes it a test and
# not a threshold fitted to the observed value.
MAX_QUADRANT_MAE = 25.0


def wait_for(proc: subprocess.Popen, token: str, timeout: float, label: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{label} exited early rc={proc.returncode}")
        line = proc.stdout.readline()
        if not line:
            continue
        sys.stderr.write(f"[{label}] {line}")
        if token in line:
            return
    raise TimeoutError(f"{label} never printed {token}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wire", choices=("jpeg", "raw"), default="jpeg")
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--port", type=int, default=6502)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()

    room = f"spike-pixel-{int(time.time())}"
    env = dict(os.environ, LK_ROOM=room, LK_W=str(args.width), LK_H=str(args.height))
    py = sys.executable

    ctx = zmq.Context()
    pull = ctx.socket(zmq.PULL)
    pull.bind(f"tcp://127.0.0.1:{args.port}")   # DataSource binds PULL, as scheme_zmq does
    pull.setsockopt(zmq.RCVTIMEO, 15000)

    pub = subprocess.Popen([py, os.path.join(HERE, "publisher.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    side = None
    try:
        wait_for(pub, "PUBLISHER_READY", 30, "pub")
        side = subprocess.Popen(
            [py, os.path.join(HERE, "sidecar.py"), "--wire", args.wire,
             "--zmq", f"tcp://127.0.0.1:{args.port}", "--max-seconds", "60"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_for(side, "SIDECAR_READY", 30, "sidecar")

        received, best = 0, None
        deadline = time.time() + 40
        while received < args.frames and time.time() < deadline:
            try:
                msg = pull.recv()
            except zmq.Again:
                break
            width, height, length = WIRE_HEADER.unpack_from(msg, 0)
            payload = msg[WIRE_HEADER.size:WIRE_HEADER.size + length]
            if args.wire == "jpeg":
                import io

                from PIL import Image
                arr = np.array(Image.open(io.BytesIO(payload)).convert("RGB"))
            else:
                arr = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)
            received += 1

            seq = pattern.read_seq(arr)
            expected = pattern.make(width, height, seq)
            result = pattern.compare(expected, arr)
            result["recovered_seq"] = seq
            if best is None or result["max_quadrant_mae"] < best["max_quadrant_mae"]:
                best = result

        # Ask the sidecar to report its timings, then read them off its stdout.
        side.terminate()
        stats = None
        try:
            out, _ = side.communicate(timeout=20)
            for line in out.splitlines():
                sys.stderr.write(f"[sidecar] {line}\n")
                if line.startswith("SIDECAR_STATS="):
                    stats = json.loads(line.split("=", 1)[1])
        except subprocess.TimeoutExpired:
            side.kill()

        verdict = {
            "wire": args.wire,
            "frames_received_on_pipeline_side": received,
            "pixel": best,
            "sidecar": stats,
        }
        ok = bool(best) and best["max_quadrant_mae"] <= MAX_QUADRANT_MAE
        verdict["result"] = "PIXEL_OK" if ok else "PIXEL_FAIL"
        print("PIXEL_VERDICT=" + json.dumps(verdict), flush=True)
        return 0 if ok else 3
    finally:
        for proc in (side, pub):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        pull.close()
        ctx.term()


if __name__ == "__main__":
    sys.exit(main())
