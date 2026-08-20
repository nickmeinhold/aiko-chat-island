"""Pipeline element module for the `webrtc://` integration proof.

The DataSource here is deliberately NOT something written for this spike. Importing
`scheme_webrtc` registers `webrtc://` in `aiko.DataScheme.LOOKUP`, and then aiko's OWN
`image_io.ImageReadZMQ` — re-exported below completely unmodified — consumes it.

That is the whole claim under test. If `webrtc://` is genuinely first-class at the URL +
registration layer, an element that has never heard of WebRTC should read from a LiveKit
room by changing one string in a pipeline definition:

    "data_sources": "(zmq://localhost:6502)"   ->   "(webrtc://my-room)"

`PatternAssert` is the acceptance gate: a real PipelineElement that receives real decoded
images from the real pipeline and checks them against the independently generated known
pattern. Without it the pipeline could "run" while carrying garbage.

NOTE ON A NAMING WART, worth putting to Andy: the element that works here is called
`ImageReadZMQ`. It is generic — its `process_frame` only calls `bytes_to_image(record)`
and never touches zmq — so the transport lives entirely in the scheme, exactly as Andy's
model says ("the DataScheme references the transport"). But the ELEMENT name still leaks a
transport it does not depend on, which is why reading from `webrtc://` currently requires
an element called `...ZMQ`. The generic element wants a transport-neutral name.
"""
from __future__ import annotations

import os
import sys
from typing import Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiko_services as aiko  # noqa: E402
import numpy as np  # noqa: E402
import pattern  # noqa: E402
import scheme_webrtc  # noqa: F401,E402  — the import IS the registration of webrtc://
# Re-exported UNMODIFIED from aiko_services. Not subclassed, not wrapped: the pipeline
# below loads these exact classes.
from aiko_services.elements.media.image_io import (  # noqa: E402,F401
    ImageOutput,
    ImageReadZMQ,
)

__all__ = ["ImageReadZMQ", "ImageOutput", "PatternAssert"]

MAX_QUADRANT_MAE = 25.0   # same budget as run_pixel.py; see FINDINGS.md F3 for its controls


class PatternAssert(aiko.PipelineElement):
    """Acceptance gate — assert real pipeline images match the known pattern."""

    def __init__(self, context: aiko.ContextPipelineElement):
        context.set_protocol("pattern_assert:0")
        context.call_init(self, "PipelineElement", context)
        self.seen = 0
        self.best = None

    def process_frame(self, stream, images) -> Tuple[aiko.StreamEvent, dict]:
        for image in images:
            arr = np.array(image.convert("RGB")) if hasattr(image, "convert") else np.asarray(image)
            self.seen += 1
            seq = pattern.read_seq(arr)
            result = pattern.compare(pattern.make(arr.shape[1], arr.shape[0], seq), arr)
            result["recovered_seq"] = seq
            if self.best is None or result["max_quadrant_mae"] < self.best["max_quadrant_mae"]:
                self.best = result
            ok = result["max_quadrant_mae"] <= MAX_QUADRANT_MAE
            print(f"PIPELINE_FRAME n={self.seen} seq={seq} "
                  f"max_quadrant_mae={result['max_quadrant_mae']} "
                  f"shape={arr.shape} {'OK' if ok else 'FAIL'}", flush=True)
            if self.seen >= int(self.get_parameter("assert_frames", 5)[0]):
                verdict = "PIPELINE_PIXEL_OK" if ok else "PIPELINE_PIXEL_FAIL"
                print(f"PIPELINE_VERDICT={verdict} frames={self.seen} best={self.best}", flush=True)
                return aiko.StreamEvent.STOP, {"diagnostic": verdict}
        return aiko.StreamEvent.OKAY, {"images": images}
